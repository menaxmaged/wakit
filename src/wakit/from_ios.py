"""Import the chats of an iPhone WhatsApp backup (ChatStorage.sqlite + Message/Media) into an Android msgstore.db.

Point --backup at a full unencrypted iPhone backup (the folder holding Manifest.db), or at WhatsApp's folder
already extracted from one (holding ChatStorage.sqlite and Message/ - see `wakit ios-backup extract`).
1:1 chats and groups are imported; statuses and broadcast lists are not. Chats are matched by jid (@lid or
@g.us), so iPhone and Android history of the same person land in one chat; missing 1:1 chats are created.
Messages already in the chat (same message id, or same sender + text/media type at about the same time, as
for text exports) are skipped, so re-running never duplicates.

Carried over: text, links, photos (with captions), videos, GIFs, voice notes/audio, stickers, documents,
contact cards, locations (as a maps link), deleted messages, and group senders. Not carried over: system
notices, calls, polls, reactions and reply quotes (those live in protobuf blobs).

Dry run by default; pass --apply to write. A timestamped backup is made before writing.

examples:
  wakit from-ios --db msgstore.db --backup ios/
  wakit from-ios --db msgstore.db --backup ios/ --apply --push --phone-root /sdcard/WhatsApp
"""
import argparse, base64, hashlib, mimetypes, os, re, shutil, sqlite3, sys, tempfile
from datetime import datetime

from . import export_import as w, ios_backup

APPLE_EPOCH = 978307200  # iOS stores dates as seconds since 2001-01-01
# iOS ZMESSAGETYPE -> Android message_type ("location" becomes a text with a maps link)
TYPES = {0: 0, 7: 0, 41: 0, 1: 1, 38: 1, 2: 3, 3: 2, 4: 4, 5: "location", 8: 9, 11: 13, 14: 15, 15: 20}
FOLDERS = {1: "WhatsApp Images", 3: "WhatsApp Video", 2: "WhatsApp Audio", 9: "WhatsApp Documents",
           13: "WhatsApp Animated Gifs", 20: "WhatsApp Stickers"}
KINDS = {0: "text", 1: "image", 2: "audio", 3: "video", 4: "contact", 9: "document", 13: "gif", 15: "deleted",
         20: "sticker"}
# a 1:1 chat as the app creates it (copied from one it made itself); pointers are filled in after the import
DIRECT_CHAT = {"hidden": 0, "archived": 0, "spam_detection": 1, "unseen_earliest_message_received_time": 0,
               "unseen_message_count": 0, "unseen_missed_calls_count": 0, "unseen_row_count": 0, "plaintext_disabled": 1,
               "vcard_ui_dismissed": 0, "change_number_notified_message_row_id": 1, "show_group_description": 0,
               "ephemeral_expiration": 0, "ephemeral_setting_timestamp": 0, "ephemeral_disappearing_messages_initiator": 0,
               "unseen_important_message_count": 0, "group_type": 0, "last_message_reaction_row_id": 0,
               "last_seen_message_reaction_row_id": 0, "unseen_message_reaction_count": 0,
               "unseen_comment_message_count": 0, "has_new_community_admin_dialog_been_acknowledged": 0,
               "history_sync_progress": 0, "chat_lock": 0, "chat_origin": "general", "participation_status": 0,
               "chat_encryption_state": 2, "group_member_count": -1, "limited_sharing": 0,
               "limited_sharing_setting_timestamp": 0, "ephemeral_after_read_duration": 0, "business_chat_state": 0}


def read_backup(root):
    """{jid: {"name", "group", "hidden", "rows"}} plus skip counts, from ChatStorage.sqlite."""
    db = os.path.join(root, "ChatStorage.sqlite")
    if not os.path.exists(db):
        w.die(f"no ChatStorage.sqlite in {root}")
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    chats = {pk: {"jid": jid, "name": name, "group": stype == 1, "hidden": bool(hidden), "rows": []}
             for pk, jid, name, stype, hidden in con.execute(
                 "SELECT Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, coalesce(ZHIDDEN, 0) FROM ZWACHATSESSION "
                 "WHERE ZSESSIONTYPE IN (0, 1) AND ZCONTACTJID IS NOT NULL")}
    skipped, names = {}, set()
    skip = lambda why: skipped.__setitem__(why, skipped.get(why, 0) + 1)
    q = """SELECT m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZTEXT, m.ZSTANZAID, gm.ZMEMBERJID,
                  mi.ZMEDIALOCALPATH, mi.ZTITLE, mi.ZVCARDNAME, mi.ZVCARDSTRING, mi.ZLATITUDE, mi.ZLONGITUDE
           FROM ZWAMESSAGE m LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
           LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
           ORDER BY m.ZMESSAGEDATE, m.Z_PK"""
    for chat, from_me, date, itype, text, key, member, path, title, vname, vcard, lat, lon in con.execute(q):
        c = chats.get(chat)
        if c is None:
            skip("status/broadcast")
            continue
        t = TYPES.get(itype)
        if t is None:
            skip("system/call/poll/other")
            continue
        if not date:
            skip("no date")
            continue
        r = {"ts": int((date + APPLE_EPOCH) * 1000), "from_me": int(bool(from_me)), "type": t, "text": None,
             "media": None, "key": key, "sender": None if from_me or not c["group"] else member}
        if c["group"] and not from_me and not member:
            skip("group message without sender")
            continue
        text = (text or "").strip() or None
        if t == 0:
            if not text:
                skip("empty")
                continue
            r["text"] = text
        elif t == "location":
            if not lat and not lon:
                skip("empty")
                continue
            r.update(type=0, text=f"📍 https://maps.google.com/?q={lat:.6f},{lon:.6f}")
        elif t == 4:
            if not vcard:
                skip("contact without card")
                continue
            fn = re.search(r"^FN[;:](.*)$", vcard, re.M)
            r.update(text=vname or (fn.group(1).strip() if fn else "Contact"), vcard=vcard)
        elif t != 15:
            caption = (title or "").strip() or None if t in (1, 3, 13) else None
            fpath = path and os.path.join(root, "Message", path)
            if not fpath or not os.path.isfile(fpath):
                skip("media file missing")
                if not caption:
                    continue
                r.update(type=0, text=caption)
            else:
                ext = fpath.rsplit(".", 1)[-1].lower()
                name = os.path.basename(fpath)
                if t == 9:  # documents keep their title as the file name
                    doc = re.sub(r'[/\\:*?"<>|]', "_", text or title or "")
                    if doc:
                        name = doc if doc.lower().endswith("." + ext) else f"{doc}.{ext}"
                    base, n = name, 1
                    while name in names:
                        n += 1
                        name = f"{base.rsplit('.', 1)[0]} ({n}).{ext}"
                names.add(name)
                folder = "WhatsApp Voice Notes" if t == 2 and ext in ("opus", "ogg") else FOLDERS[t]
                mime = w.MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
                r.update(text=caption, media={"name": name, "path": fpath, "mime": mime, "folder": folder})
        c["rows"].append(r)
    return {c["jid"]: c for c in chats.values() if c["rows"]}, skipped


def ensure_jid(cur, raw):
    row = cur.execute("SELECT _id FROM jid WHERE raw_string=?", (raw,)).fetchone()
    if row:
        return row[0]
    user, server = raw.split("@", 1)
    jtype = {"lid": 18, "s.whatsapp.net": 0, "g.us": 1}.get(server, 0)  # what the app uses for chat/user jids
    return cur.execute("INSERT INTO jid(user, server, agent, device, type, raw_string) VALUES(?,?,0,0,?,?)",
                       (user, server, jtype, raw)).lastrowid


def create_direct_chat(cur, jid_id, first_ts):
    have = w.chat_cols(cur)
    vals = {c: v for c, v in DIRECT_CHAT.items() if c in have}
    vals.update(jid_row_id=jid_id, account_jid_row_id=jid_id, created_timestamp=first_ts, sort_timestamp=first_ts)
    return cur.execute(f'INSERT INTO chat({",".join(vals)}) VALUES({",".join("?" * len(vals))})',
                       list(vals.values())).lastrowid


def insert(cur, chat_id, rows, media_out, folders, tmp):
    first = cur.execute("SELECT coalesce(max(sort_id), 0) + 1 FROM message").fetchone()[0]
    chat_jid = cur.execute("SELECT jid_row_id FROM chat WHERE _id=?", (chat_id,)).fetchone()[0]
    for i, r in enumerate(rows):
        sender = ensure_jid(cur, r["sender"]) if r["sender"] else 0
        rid = cur.execute(
            """INSERT INTO message(chat_row_id, from_me, key_id, sender_jid_row_id, status, broadcast, recipient_count,
                   origination_flags, origin, timestamp, received_timestamp, receipt_server_timestamp, message_type,
                   text_data, starred, lookup_tables, message_add_on_flags, view_mode, sort_id)
               VALUES(?,?,?,?,?,0,0,65536,0,?,0,-1,?,?,0,0,0,0,?)""",
            (chat_id, r["from_me"], r["key"] or w.new_key(), sender, 5 if r["from_me"] else 0, r["ts"], r["type"],
             r["text"], first + i)).lastrowid
        if r["type"] == 15:
            cur.execute("INSERT INTO message_revoked(message_row_id, revoked_key_id, revoke_timestamp) VALUES(?,?,?)",
                        (rid, w.new_key(), r["ts"]))
        if r.get("vcard"):
            cur.execute("INSERT INTO message_vcard(message_row_id, vcard) VALUES(?,?)", (rid, r["vcard"]))
        if r["media"]:
            md = r["media"]
            data = open(md["path"], "rb").read()
            rel = f"Media/{folders.get(md['folder'], md['folder'])}/{md['name']}"
            cur.execute(
                """INSERT INTO message_media(message_row_id, chat_row_id, autotransfer_retry_enabled, transferred,
                       file_path, file_size, mime_type, file_length, media_name, file_hash)
                   VALUES(?,?,0,1,?,?,?,?,?,?)""",
                (rid, chat_id, rel, len(data), md["mime"], len(data), md["name"],
                 base64.b64encode(hashlib.sha256(data).digest()).decode()))
            w.write_media_details(cur, rid, w.media_details(r["type"], md["path"], tmp))
            if media_out:
                dst = os.path.join(media_out, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(md["path"], dst)
        if r["text"]:
            who = "1" if r["from_me"] else (w.b36(sender + 10) if sender else "0")
            cur.execute("INSERT INTO message_ftsv2(docid, content, fts_jid, fts_namespace) VALUES(?,?,?,'')",
                        (rid, r["text"], f"{who} {w.b36(chat_jid + 10)}"))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Android msgstore.db to import into")
    p.add_argument("--backup", required=True, help="folder with the iPhone's ChatStorage.sqlite and Message/, or a full unencrypted iPhone backup (holds Manifest.db)")
    p.add_argument("--media-out", help="folder to copy media into, laid out as Media/... (default: <db dir>/ios-media)")
    p.add_argument("--push", action="store_true", help="with --apply: copy the media to the phone over adb")
    p.add_argument("--package", default="com.whatsapp", help="app package, for --push (default: com.whatsapp)")
    p.add_argument("--phone-root", help="app media root on the phone, for --push (e.g. /sdcard/WhatsApp)")
    p.add_argument("--apply", action="store_true", help="actually write (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.backup = ios_backup.resolve(a.backup)
    if not os.path.exists(a.db):
        w.die(f"{a.db} not found")

    chats, skipped = read_backup(a.backup)
    media_out = a.media_out or os.path.join(os.path.dirname(os.path.abspath(a.db)), "ios-media")
    root, folders = (None, {})
    if a.push and a.apply:
        root, folders = w.phone_media_layout(a.package, a.phone_root)
        print(f"phone:  media root {root}")

    con = sqlite3.connect(a.db)
    bak = w.backup(a.db) if a.apply else None
    cur = con.cursor()
    cur.execute("BEGIN")
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    totals, counts, created, already, report = 0, {}, 0, 0, []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for jid, c in sorted(chats.items(), key=lambda kv: -len(kv[1]["rows"])):
                rows = c["rows"]
                hit = cur.execute("SELECT c._id, c.hidden FROM chat c JOIN jid j ON j._id=c.jid_row_id "
                                  "WHERE j.raw_string=?", (jid,)).fetchone()
                if hit:
                    chat_id = hit[0]
                    keys = {k for (k,) in cur.execute("SELECT key_id FROM message WHERE chat_row_id=?", (chat_id,))}
                    rows = [r for r in rows if r["key"] not in keys]
                    # the chat may already hold the same messages from a text export: other phone's clock, often
                    # minute precision, and the iPhone dates messages by when it received them (sometimes minutes
                    # late) - so allow 2 min for the same text (15 min if 6+ characters), 61s for the same media type
                    rows = w.drop_existing(con, chat_id, rows, 120000, 61000, long_window=15 * 60000)
                elif c["group"]:
                    print(f"skip:   group {c['name']} ({jid}) has no chat in the db")
                    continue
                else:
                    chat_id = None
                already += len(c["rows"]) - len(rows)
                if not rows:
                    continue
                if chat_id is None:
                    chat_id = create_direct_chat(cur, ensure_jid(cur, jid), rows[0]["ts"])
                    created += 1
                elif hit[1] and not c["hidden"]:
                    cur.execute("UPDATE chat SET hidden=0 WHERE _id=?", (chat_id,))
                insert(cur, chat_id, rows, media_out if a.apply else None, folders, tmp)
                cur.execute("UPDATE chat SET created_timestamp=min(coalesce(nullif(created_timestamp, 0), ?), ?) "
                            "WHERE _id=?", (rows[0]["ts"], rows[0]["ts"], chat_id))
                report.append((c["name"] or jid, chat_id, len(rows), hit is None))
                totals += len(rows)
                for r in rows:
                    counts[KINDS[r["type"]]] = counts.get(KINDS[r["type"]], 0) + 1
            if totals:
                w.renumber_sort_ids(cur)
                for _, chat_id, _, _ in report:
                    w.point_chat_at_last(cur, chat_id)
        print(f"backup: {sum(len(c['rows']) for c in chats.values())} messages in {len(chats)} chats; skipped "
              + ", ".join(f"{v} {k}" for k, v in skipped.items()))
        print(f"skip:   {already} already in the db")
        print(f"import: {totals} messages into {len(report)} chats ({created} new): "
              + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
        for name, chat_id, n, new in report[:25]:
            print(f"        {n:6} {name} (chat {chat_id}{', new' if new else ''})")
        if len(report) > 25:
            print(f"        ... and {len(report) - 25} more chats")
        if not totals:
            print("nothing to import")
            con.rollback()
            if bak:
                shutil.rmtree(bak)
            return
        ok = cur.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity check failed: {ok}")
        if not a.apply:
            con.rollback()
            print("\ndry run - nothing written. Re-run with --apply to import.")
            return
        print(f"backup: {bak}")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"done:   imported {totals} messages")
    if counts.keys() - {"text", "deleted", "contact"}:
        if root:
            w.push_media(root, media_out)
        else:
            print(f"media:  copied to {media_out}/Media - copy it into the app's media folder on the phone")


if __name__ == "__main__":
    main()
