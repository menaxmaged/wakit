"""Merge the chat history of an older Android msgstore.db into a newer one.

Copies chats, messages and what hangs off them (media rows, thumbnails, voice-note waveforms,
quotes, mentions, reactions, polls, edits, deletions, links, system messages, call logs) and the
search index. Messages already in the new database (same chat + key id) are skipped, so re-running
is safe.

The newer app keys 1:1 chats by @lid instead of the phone number, so old phone-number chats and
senders are moved onto their @lid (from the new jid_map, else the old one) - otherwise they would
show up as a second, duplicate chat. Statuses, channels and broadcast lists are not copied.
Receipts, devices and group participant lists are left to the new app (it re-syncs them).

Dry run by default; pass --apply to write. A timestamped backup is made before writing.
The old database is only ever read (from a temporary copy).

examples:
  wakit merge --old old/msgstore.db --new new/msgstore.db
  wakit merge --old old/msgstore.db --new new/msgstore.db --apply
"""
from . import doctor
from .export_import import has_fts
import argparse, os, shutil, sqlite3, sys, tempfile
from datetime import datetime

SKIP_SERVERS = {"broadcast", "newsletter", "status_me"}  # status@broadcast, channels, broadcast lists

# Child tables copied row by row: {table: {column: mapping}}. Mappings:
#   M message id   C chat id   J jid (phone number -> @lid)   R jid as-is   A add-on id   P poll option id
#   pk  own integer key, gets a fresh value   A* / P*  own key, fresh value recorded in the A / P map
# Rows whose M/C/A/P can't be mapped (message skipped, etc.) are dropped. Order matters (A/P before use).
CHILDREN = [
    ("message_media", {"message_row_id": "M", "chat_row_id": "C"}),
    ("message_thumbnail", {"message_row_id": "M"}),
    ("audio_data", {"message_row_id": "M"}),
    ("message_text", {"message_row_id": "M"}),
    ("message_revoked", {"message_row_id": "M", "admin_jid_row_id": "J"}),
    ("message_system", {"message_row_id": "M"}),
    ("message_system_group", {"message_row_id": "M"}),
    ("message_system_initial_privacy_provider", {"message_row_id": "M"}),
    ("message_system_chat_participant", {"message_row_id": "M", "user_jid_row_id": "J"}),
    ("message_secret", {"message_row_id": "M"}),
    ("message_details", {"message_row_id": "M"}),
    ("message_forwarded", {"message_row_id": "M"}),
    ("message_edit_info", {"message_row_id": "M"}),
    ("message_ephemeral_setting", {"message_row_id": "M"}),
    ("message_quoted", {"message_row_id": "M", "chat_row_id": "C", "parent_message_chat_row_id": "C",
                        "sender_jid_row_id": "J"}),
    ("message_quoted_text", {"message_row_id": "M"}),
    ("message_quoted_media", {"message_row_id": "M"}),
    ("message_quoted_mentions", {"_id": "pk", "message_row_id": "M", "jid_row_id": "R"}),
    ("message_mentions", {"_id": "pk", "message_row_id": "M", "jid_row_id": "R"}),  # text says "@<number>"
    ("message_link", {"_id": "pk", "chat_row_id": "C", "message_row_id": "M"}),
    ("message_template", {"message_row_id": "M"}),
    ("message_template_button", {"_id": "pk", "message_row_id": "M"}),
    ("message_poll", {"message_row_id": "M"}),
    ("message_poll_option", {"_id": "P*", "message_row_id": "M"}),
    ("message_add_on", {"_id": "A*", "chat_row_id": "C", "sender_jid_row_id": "J", "parent_message_row_id": "M"}),
    ("message_add_on_reaction", {"message_add_on_row_id": "A"}),
    ("message_add_on_poll_vote", {"message_add_on_row_id": "A"}),
    ("message_add_on_poll_vote_selected_option", {"_id": "pk", "message_add_on_row_id": "A",
                                                  "message_poll_option_id": "P"}),
    ("missed_call_logs", {"_id": "pk", "message_row_id": "M", "group_jid_row_id": "J"}),
]
# chat columns that point at messages; for chats we create they're set to the chat's last message
CHAT_MSG_COLS = ["display_message_row_id", "last_message_row_id", "last_read_message_row_id",
                 "last_read_receipt_sent_message_row_id", "last_important_message_row_id"]
CHAT_SORT_COLS = ["display_message_sort_id", "last_message_sort_id", "last_read_message_sort_id",
                  "last_read_receipt_sent_message_sort_id"]
CHAT_RESET = {"last_message_reaction_row_id": None, "last_seen_message_reaction_row_id": None,
              "last_read_ephemeral_message_row_id": None, "change_number_notified_message_row_id": -1,
              "unseen_earliest_message_received_time": 0, "unseen_message_count": 0, "unseen_missed_calls_count": 0,
              "unseen_row_count": 0, "unseen_important_message_count": 0, "unseen_message_reaction_count": 0}
# chat state the app fills in itself (several columns don't exist in older schemas); for chats we create these are
# taken from the app's own visible group chats, else these fallbacks. 1:1 chats leave the GROUP_STATE ones empty.
CHAT_STATE = {"chat_encryption_state": 2, "limited_sharing": 0, "business_chat_state": 0, "plaintext_disabled": 1,
              "spam_detection": 1}
GROUP_STATE = {"participation_status": 2, "group_member_count": -1}


def die(msg):
    sys.exit(f"error: {msg}")


def cols(con, table, schema="main"):
    return [r[1] for r in con.execute(f'PRAGMA {schema}.table_info("{table}")')]


def b36(n):
    s, digits = "", "0123456789abcdefghijklmnopqrstuvwxyz"
    while True:
        n, r = divmod(n, 36)
        s = digits[r] + s
        if not n:
            return s


class Jids:
    """Maps old jid row ids to new ones, inserting jids the new db doesn't have yet."""

    def __init__(self, cur):
        self.cur = cur
        self.old = {r[0]: r for r in cur.execute("SELECT _id, user, server, agent, device, type, raw_string FROM o.jid")}
        self.old_by_raw = {r[6]: r for r in self.old.values()}
        self.new_by_raw = dict(cur.execute("SELECT raw_string, _id FROM jid WHERE raw_string IS NOT NULL"))
        q = "SELECT p.raw_string, l.raw_string FROM {0}jid_map m JOIN {0}jid l ON l._id=m.lid_row_id JOIN {0}jid p ON p._id=m.jid_row_id"
        new_map = dict(cur.execute(q.format("")))
        mapped_lids = set(new_map.values())
        self.lid_of = dict(new_map)
        self.from_old_map = {}  # pn -> lid learned only from the old db (new db gets a jid_map row for it)
        for pn, lid in cur.execute(q.format("o.")):
            if pn not in self.lid_of and lid not in mapped_lids:
                self.lid_of[pn] = self.from_old_map[pn] = lid
        self.added = 0
        self.mapped = set()

    def raw(self, oid):
        r = self.old.get(oid)
        return r and r[6]

    def get(self, oid, canon=True):
        if oid is None or oid <= 0 or oid not in self.old:
            return oid
        raw = self.old[oid][6]
        if canon and raw in self.lid_of:
            self.mapped.add(raw)
            raw = self.lid_of[raw]
        return self._ensure(raw)

    def _ensure(self, raw):
        if raw in self.new_by_raw:
            return self.new_by_raw[raw]
        r = self.old_by_raw[raw]
        nid = self.cur.execute("INSERT INTO jid(user, server, agent, device, type, raw_string) VALUES(?,?,?,?,?,?)",
                               r[1:]).lastrowid
        self.new_by_raw[raw] = nid
        self.added += 1
        return nid

    def finish(self):
        """Record pn<->lid pairs we relied on that only the old db knew, plus old @lid display names."""
        sort = self.cur.execute("SELECT coalesce(max(sort_id), 0) FROM jid_map").fetchone()[0]
        n = 0
        for pn in sorted(self.mapped & set(self.from_old_map)):
            sort += 1
            self.cur.execute("INSERT OR IGNORE INTO jid_map(lid_row_id, jid_row_id, sort_id) VALUES(?,?,?)",
                             (self._ensure(self.from_old_map[pn]), self._ensure(pn), sort))
            n += 1
        names = 0
        for lid_raw, name in self.cur.execute(
                "SELECT j.raw_string, d.display_name FROM o.lid_display_name d JOIN o.jid j ON j._id=d.lid_row_id").fetchall():
            if lid_raw in self.new_by_raw:
                names += self.cur.execute("INSERT OR IGNORE INTO lid_display_name(lid_row_id, display_name) VALUES(?,?)",
                                          (self.new_by_raw[lid_raw], name)).rowcount
        return n, names


def copy_rows(cur, table, spec, maps, jids, or_ignore=False):
    """Copy o.<table> into <table> remapping columns per spec. Returns rows copied."""
    if not cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    old_cols = cols(cur, table, "o")
    common = [c for c in old_cols if c in set(cols(cur, table))]
    own = next((c for c, k in spec.items() if k in ("pk", "A*", "P*")), None)
    out_cols = [c for c in common if c != own]
    sql = f'INSERT {"OR IGNORE " if or_ignore else ""}INTO "{table}"({",".join(out_cols)}) VALUES({",".join("?" * len(out_cols))})'
    n = 0
    for row in cur.execute(f'SELECT {",".join(common)} FROM o."{table}"').fetchall():
        r = dict(zip(common, row))
        ok = True
        for c, kind in spec.items():
            if c not in r or c == own:
                continue
            v = r[c]
            if kind in ("M", "C", "A", "P"):
                if v is None or v <= 0:
                    if kind in ("M", "A"):
                        ok = False
                    continue
                if v not in maps[kind]:
                    ok = False
                    break
                r[c] = maps[kind][v]
            elif kind in ("J", "R"):
                r[c] = jids.get(v, canon=kind == "J")
        if not ok:
            continue
        if table == "message_ephemeral_setting" and r.get("user_jid_row_id_csv"):
            r["user_jid_row_id_csv"] = ",".join(str(jids.get(int(x))) for x in r["user_jid_row_id_csv"].split(",") if x.strip())
        res = cur.execute(sql, [r[c] for c in out_cols])
        if not res.rowcount:
            continue
        new_id = res.lastrowid
        if own and spec[own] != "pk":
            maps[spec[own][0]][r[own]] = new_id
        n += 1
    return n


def backup(db):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = f"{db}.bak-{stamp}"
    os.makedirs(dst)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            shutil.copy2(db + suffix, os.path.join(dst, os.path.basename(db) + suffix))
    return dst


def chat_defaults(cur):
    """(1:1 defaults, group defaults) for CHAT_STATE / GROUP_STATE columns, as the new app sets them."""
    have = set(cols(cur, "chat"))
    names = [c for c in {**CHAT_STATE, **GROUP_STATE} if c in have]
    sel = ",".join("c." + c for c in names)
    row = cur.execute(f"""SELECT {sel} FROM chat c JOIN jid j ON j._id=c.jid_row_id WHERE j.server='g.us' AND c.hidden=0
                          GROUP BY {sel} ORDER BY count(*) DESC LIMIT 1""").fetchone()
    d = {c: v if v is not None else {**CHAT_STATE, **GROUP_STATE}[c] for c, v in zip(names, row or [None] * len(names))}
    return {c: v for c, v in d.items() if c in CHAT_STATE}, d


def shift_sort_ids(cur, n):
    """Move the new db's messages (and the chat columns pointing at their sort ids) up by n."""
    for c in CHAT_SORT_COLS:
        if c in set(cols(cur, "chat")):
            cur.execute(f"UPDATE chat SET {c}={c}+? WHERE {c} IN (SELECT sort_id FROM message)", (n,))
    cur.execute("UPDATE message SET sort_id=sort_id+?", (n,))


def renumber_sort_ids(cur):
    """Number every message 0..n-1 in time order and repoint the chats' *_sort_id columns. The app gives each
    new message sort_id = its row id, so sort ids must stay at or below the highest row id - otherwise new
    messages sort above older ones."""
    order = cur.execute("SELECT _id, sort_id FROM message ORDER BY timestamp > 0, "
                        "CASE WHEN timestamp > 0 THEN timestamp ELSE sort_id END, sort_id, _id").fetchall()
    cur.executemany("UPDATE message SET sort_id=? WHERE _id=?", [(i, mid) for i, (mid, s) in enumerate(order) if s != i])
    have = set(cols(cur, "chat"))
    for rc, sc in zip(CHAT_MSG_COLS, CHAT_SORT_COLS):  # display, last, last_read, last_read_receipt_sent
        if rc in have and sc in have:
            cur.execute(f"UPDATE chat SET {sc}=(SELECT sort_id FROM message WHERE _id=chat.{rc}) "
                        f"WHERE {rc} IN (SELECT _id FROM message)")


def fuzzy_skips(cur, msg_cols, maps, existing, skip_types):
    """Old message ids that are already in the new db under another key - e.g. history that was imported from a
    text export on one phone and arrived again another way (same side, same text or media type, close in time)."""
    from . import export_import as w
    per_chat = {}
    for oid, chat, from_me, key, ts, mtype, text in cur.execute(
            "SELECT _id, chat_row_id, from_me, key_id, timestamp, message_type, text_data FROM o.message WHERE timestamp>0"):
        c = maps["C"].get(chat)
        if c is None or (c, from_me, key) in existing or mtype in skip_types:
            continue
        per_chat.setdefault(c, []).append({"oid": oid, "ts": ts, "from_me": from_me, "type": mtype, "text": text})
    skip = set()
    for c, rows in per_chat.items():
        rows.sort(key=lambda r: r["ts"])
        kept = {r["oid"] for r in w.drop_existing(cur, c, rows, 120000, 61000, 15 * 60000)}
        skip |= {r["oid"] for r in rows} - kept
    return skip


def merge(cur, report, fuzzy=False, skip_types=()):
    jids = Jids(cur)
    maps = {"M": {}, "C": {}, "A": {}, "P": {}}

    # ---- chats
    chat_cols = [c for c in cols(cur, "chat", "o") if c in set(cols(cur, "chat")) and c != "_id"]
    created, merged, hidden_new = set(), {}, {}
    direct_defaults, group_defaults = chat_defaults(cur)
    old_chats = cur.execute(f"""SELECT c._id, {",".join("c." + x for x in chat_cols)} FROM o.chat c
                                JOIN o.jid j ON j._id=c.jid_row_id
                                WHERE EXISTS(SELECT 1 FROM o.message m WHERE m.chat_row_id=c._id)
                                ORDER BY c._id""").fetchall()
    for row in old_chats:
        oid, r = row[0], dict(zip(chat_cols, row[1:]))
        old_jid = jids.old[r["jid_row_id"]]
        if old_jid[2] in SKIP_SERVERS:
            report["skipped chats"] = report.get("skipped chats", 0) + 1
            continue
        njid = jids.get(r["jid_row_id"])
        hit = cur.execute("SELECT _id, hidden FROM chat WHERE jid_row_id=?", (njid,)).fetchone()
        if hit:
            maps["C"][oid] = hit[0]
            if hit[0] not in created:
                merged[hit[0]] = old_jid[6]
                if hit[1] and not r["hidden"]:
                    hidden_new[hit[0]] = True
            continue
        r.update(CHAT_RESET)
        r.update({c: None for c in CHAT_MSG_COLS + CHAT_SORT_COLS if c in r})
        r["jid_row_id"] = njid
        if "account_jid_row_id" in r:  # newer source schemas have it; it is the chat's own jid, so remap it too
            r["account_jid_row_id"] = njid
        for c, v in (group_defaults if old_jid[2] == "g.us" else direct_defaults).items():
            if r.get(c) is None:
                r[c] = v
        out = [c for c in r if c in set(cols(cur, "chat"))]
        values = [r[c] for c in out]
        extra = ", account_jid_row_id" if "account_jid_row_id" not in out else ""
        nid = cur.execute(f'INSERT INTO chat({",".join(out)}{extra}) VALUES({",".join("?" * len(out))}{", ?" if extra else ""})',
                          values + ([njid] if extra else [])).lastrowid
        maps["C"][oid] = nid
        created.add(nid)

    # ---- messages: keep old order, placed before everything in the new db. Old messages take sort ids
    # from 1 up and the new db's own messages are moved up past them (the app never uses negative sort ids).
    msg_cols = [c for c in cols(cur, "message", "o") if c in set(cols(cur, "message")) and c != "_id"]
    old_min, old_max = cur.execute("SELECT coalesce(min(sort_id), 0), coalesce(max(sort_id), 0) FROM o.message").fetchone()
    new_min = cur.execute("SELECT coalesce(min(sort_id), 0) FROM message").fetchone()[0]
    offset, shifted = 1 - old_min, False
    existing = {}
    for nid, chat, from_me, key in cur.execute("SELECT _id, chat_row_id, from_me, key_id FROM message"):
        existing[(chat, from_me, key)] = nid
    had_messages = {chat for chat, _, _ in existing}
    same_elsewhere = fuzzy_skips(cur, msg_cols, maps, existing, skip_types) if fuzzy else set()
    sql = f'INSERT INTO message({",".join(msg_cols)}) VALUES({",".join("?" * len(msg_cols))})'
    inserted, dupes = [], 0
    for row in cur.execute(f'SELECT _id, {",".join(msg_cols)} FROM o.message ORDER BY sort_id').fetchall():
        oid, r = row[0], dict(zip(msg_cols, row[1:]))
        if r["chat_row_id"] not in maps["C"]:
            continue
        if r["message_type"] in skip_types:
            report["skipped types"] = report.get("skipped types", 0) + 1
            continue
        if oid in same_elsewhere:
            dupes += 1
            continue
        r["chat_row_id"] = maps["C"][r["chat_row_id"]]
        k = (r["chat_row_id"], r["from_me"], r["key_id"])
        if k in existing:
            dupes += 1  # already there; its children are too
            continue
        r["sender_jid_row_id"] = jids.get(r["sender_jid_row_id"])
        if not shifted:
            shift_sort_ids(cur, old_max + offset + 1 - new_min)
            shifted = True
        r["sort_id"] = r["sort_id"] + offset
        nid = cur.execute(sql, [r[c] for c in msg_cols]).lastrowid
        existing[k] = maps["M"][oid] = nid
        inserted.append((nid, r))

    # ---- everything attached to messages
    for table, spec in CHILDREN:
        n = copy_rows(cur, table, spec, maps, jids)
        if n:
            report["rows"][table] = n

    # ---- search index (docid = message _id, fts_jid = "<sender> <chat>" as base36(jid id + 10))
    chat_jid = dict(cur.execute("SELECT _id, jid_row_id FROM chat"))
    by_id = {nid: r for nid, r in inserted}
    fts = 0
    both = has_fts(cur) and has_fts(cur, "o")  # backups have no search index; the app rebuilds it
    for docid, content, ns in cur.execute("SELECT docid, content, fts_namespace FROM o.message_ftsv2").fetchall() if both else []:
        nid = maps["M"].get(docid)
        if nid not in by_id:
            continue
        r = by_id[nid]
        sender = "1" if r["from_me"] else (b36(r["sender_jid_row_id"] + 10) if (r["sender_jid_row_id"] or 0) > 0 else "0")
        cur.execute("INSERT INTO message_ftsv2(docid, content, fts_jid, fts_namespace) VALUES(?,?,?,?)",
                    (nid, content, f"{sender} {b36(chat_jid[r['chat_row_id']] + 10)}", ns or ""))
        fts += 1
    report["rows"]["message_ftsv2"] = fts

    # ---- call log (not tied to messages; its unique key skips calls already there)
    n = copy_rows(cur, "call_log", {"_id": "pk", "jid_row_id": "J", "group_jid_row_id": "J",
                                    "call_creator_device_jid_row_id": "R"}, maps, jids, or_ignore=True)
    if n:
        report["rows"]["call_log"] = n

    # ---- chat pointers
    touched = {r["chat_row_id"] for _, r in inserted}
    for cid in touched:
        first_ts = cur.execute("SELECT min(timestamp) FROM message WHERE chat_row_id=? AND timestamp>0", (cid,)).fetchone()[0]
        if cid in created or cid not in had_messages:
            last_id, last_sort, last_ts = cur.execute(
                "SELECT _id, sort_id, timestamp FROM message WHERE chat_row_id=? ORDER BY sort_id DESC LIMIT 1", (cid,)).fetchone()
            sets = {c: last_id for c in CHAT_MSG_COLS} | {c: last_sort for c in CHAT_SORT_COLS}
            sets["sort_timestamp"] = last_ts
            sets = {c: v for c, v in sets.items() if c in set(cols(cur, "chat"))}
            cur.execute(f'UPDATE chat SET {",".join(c + "=?" for c in sets)} WHERE _id=?', [*sets.values(), cid])
        if first_ts:
            cur.execute("UPDATE chat SET created_timestamp=min(coalesce(nullif(created_timestamp,0), ?), ?) WHERE _id=?",
                        (first_ts, first_ts, cid))
        if hidden_new.get(cid):
            cur.execute("UPDATE chat SET hidden=0 WHERE _id=?", (cid,))

    if inserted:
        renumber_sort_ids(cur)
    for cid in created - touched:  # chats made for messages that all turned out to be there already
        cur.execute("DELETE FROM chat WHERE _id=?", (cid,))
    jm, names = jids.finish()
    report.update(created=len(created & touched), merged={c: merged[c] for c in merged if c in touched},
                  unhidden=sum(1 for c in hidden_new if c in touched), messages=len(inserted), dupes=dupes,
                  jids_added=jids.added, jid_map_added=jm, names_added=names, pn_to_lid=len(jids.mapped))
    return inserted


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--old", required=True, help="msgstore.db to copy history from (read only)")
    p.add_argument("--new", required=True, help="msgstore.db to merge into")
    p.add_argument("--fuzzy", action="store_true", help="also skip messages already in the new db under another "
                                                         "message id (same side and text/media type, close in time)")
    p.add_argument("--skip-types", default="", help="comma-separated message_type values not to copy")
    p.add_argument("--apply", action="store_true", help="actually write (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    for f in (a.old, a.new):
        if not os.path.exists(f):
            die(f"{f} not found")

    with tempfile.TemporaryDirectory() as tmp:
        # read the old db (with its WAL) from a copy so it is never modified
        old = os.path.join(tmp, "old.db")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(a.old + suffix):
                shutil.copy2(a.old + suffix, old + suffix)

        for path, label in ((old, a.old), (a.new, a.new)):
            check = sqlite3.connect(path)
            doctor.require(check, "android", label)
            check.close()
        bak = backup(a.new) if a.apply else None
        con = sqlite3.connect(a.new, isolation_level=None)
        con.execute("ATTACH ? AS o", (old,))
        cur = con.cursor()
        report = {"rows": {}}
        cur.execute("BEGIN")
        try:
            skip_types = {int(x) for x in a.skip_types.split(",") if x.strip()}
            inserted = merge(cur, report, fuzzy=a.fuzzy, skip_types=skip_types)
            ok = cur.execute("PRAGMA main.integrity_check").fetchone()[0]
            if ok != "ok":
                raise RuntimeError(f"integrity check failed: {ok}")
            fk = cur.execute("PRAGMA main.foreign_key_check").fetchall()
            if fk:
                raise RuntimeError(f"foreign key check failed: {fk[:5]}")
        except BaseException:
            con.rollback()
            if bak:
                shutil.rmtree(bak)
            raise

        fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d") if ms else "?"
        ts = [r["timestamp"] for _, r in inserted if (r["timestamp"] or 0) > 0]
        print(f"messages: {report['messages']} to copy" + (f" ({fmt(min(ts))} -> {fmt(max(ts))})" if ts else "")
              + f", {report['dupes']} already in new db")
        print(f"chats:    {report['created']} new, {len(report['merged'])} merged into existing chats, "
              f"{report.get('skipped chats', 0)} skipped (status/channels/broadcast lists)")
        for cid, raw in report["merged"].items():
            print(f"          merged {raw} -> chat {cid}")
        if report["unhidden"]:
            print(f"          {report['unhidden']} hidden chats in new db un-hidden because old history was added")
        print(f"contacts: {report['pn_to_lid']} phone numbers moved to their @lid, {report['jids_added']} jids added, "
              f"{report['jid_map_added']} number<->lid links added, {report['names_added']} lid names added")
        print("rows:     " + ", ".join(f"{t} {n}" for t, n in report["rows"].items()))

        if not a.apply:
            con.rollback()
            print("\ndry run - nothing written. Re-run with --apply to merge.")
            return
        con.commit()
        con.execute("DETACH o")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        print(f"\nbackup:   {bak}\ndone.")
        if report["rows"].get("message_media"):
            print("media:    copy the old phone's Media/ folders into the new app's media folder - the database only "
                  "points at the files (Media/WhatsApp Images/..., etc.)")


if __name__ == "__main__":
    main()
