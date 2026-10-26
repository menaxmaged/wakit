"""Put the chats of an Android msgstore.db into an iPhone backup, so restoring the backup brings them to the iPhone.

Point --backup at a *copy* of an unencrypted iPhone backup (the folder with Manifest.db, made by Finder, iTunes or
dr.fone) whose WhatsApp is the same account. The backup's ChatStorage.sqlite is kept and the Android messages it
lacks are added to it: chats are matched by jid (@lid / @g.us, with phone-number jids mapped through jid_map), and a
message already on the iPhone (same message id, or same sender + text/media type at about the same time, as for
text exports) is skipped, so the result has no duplicates. Missing 1:1 chats and groups are created.

Carried over: text, photos/videos/GIFs (with captions), voice notes/audio, stickers, documents, contact cards,
locations, deleted messages, stars, group senders and reply quotes. Not carried over: system notices, calls, polls
and reactions. Media files come from --media (a copy of the phone's /sdcard/WhatsApp, holding Media/); photo and
video thumbnails are made with the macOS tools sips and qlmanage (skipped on other systems).

Dry run by default; pass --apply to write into the backup (keep the original backup somewhere else).

examples:
  wakit to-ios --db msgstore.db --backup iphone-backup-copy/ --media tablet-media/
  wakit to-ios --db msgstore.db --backup iphone-backup-copy/ --media tablet-media/ --apply
"""
from . import ios_backup
import argparse, base64, hashlib, os, plistlib, re, shutil, sqlite3, subprocess, sys, tempfile, time, uuid
from datetime import datetime

APPLE_EPOCH = 978307200  # iOS stores dates as seconds since 2001-01-01
DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
# Android message_type -> iOS ZMESSAGETYPE
TYPES = {0: 0, 1: 1, 2: 3, 3: 2, 4: 4, 5: 5, 9: 8, 13: 11, 15: 14, 20: 15}
# iOS ZMESSAGETYPE -> Android message_type, for matching messages already on the iPhone (as in wakit from-ios)
BACK = {0: 0, 7: 0, 41: 0, 5: 0, 1: 1, 38: 1, 2: 3, 3: 2, 4: 4, 8: 9, 11: 13, 14: 15, 15: 20}
KINDS = {0: "text", 1: "image", 2: "audio", 3: "video", 4: "contact", 5: "location", 9: "document", 13: "gif",
         15: "deleted", 20: "sticker"}
MEDIA = {1, 2, 3, 9, 13, 20}
SECTION = {1, 2, 8}  # iOS types listed in the chat's media gallery (ZMEDIASECTIONID = "yyyy-mm")


def die(msg):
    sys.exit(f"error: {msg}")


# --- protobuf, just enough to write the small blobs the app keeps next to each message ---
def pb_varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb(fields):
    """[(field, int | str | bytes)] -> protobuf bytes"""
    out = b""
    for f, v in fields:
        if isinstance(v, int):
            out += pb_varint(f << 3) + pb_varint(v)
        else:
            v = v.encode() if isinstance(v, str) else v
            out += pb_varint(f << 3 | 2) + pb_varint(len(v)) + v
    return out


def push_blob(from_me, ts, me):
    """ZPUSHNAME: the same fields the app writes for a sent / received message"""
    s = int(ts)
    if from_me:
        return base64.b64encode(pb([(1, s), (3, me), (4, 1), (24, 0), (37, ""), (47, 1)])).decode()
    return base64.b64encode(pb([(1, s), (4, 0), (9, 1), (18, 0), (30, 2), (43, int(ts * 1e6))])).decode()


def quote_blob(q):
    """ZMETADATA of a reply: quoted message id (5), its sender (6) and its text (19 -> conversation)"""
    fields = [(5, q["key"]), (6, q["sender"])]
    if q["text"]:
        fields.append((19, pb([(1, q["text"])])))
    return pb(fields)


# --- the Android side ---
def read_android(db, wa_db):
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    raw = dict(con.execute("SELECT _id, raw_string FROM jid"))
    lid_of, pn_of = {}, {}
    for lid, pn in con.execute("SELECT lid_row_id, jid_row_id FROM jid_map"):
        if raw.get(lid) and raw.get(pn):
            lid_of[raw[pn]], pn_of[raw[lid]] = raw[lid], raw[pn]
    names = {}
    if wa_db and os.path.exists(wa_db):
        wcon = sqlite3.connect(f"file:{wa_db}?immutable=1", uri=True)
        names = {j: n for j, n in wcon.execute(
            "SELECT jid, coalesce(nullif(display_name, ''), nullif(wa_name, '')) FROM wa_contacts") if n}
    chats = []
    for cid, jid, server, subject, archived, hidden in con.execute(
            "SELECT c._id, j.raw_string, j.server, c.subject, c.archived, c.hidden FROM chat c "
            "JOIN jid j ON j._id = c.jid_row_id WHERE j.server IN ('lid', 's.whatsapp.net', 'g.us')"):
        group = server == "g.us"
        jid = lid_of.get(jid, jid)
        pn = None if group else pn_of.get(jid, jid if jid.endswith("@s.whatsapp.net") else None)
        chats.append({"id": cid, "jid": jid, "pn": pn, "group": group, "archived": archived or 0,
                      "hidden": hidden or 0, "name": subject if group else names.get(pn) or names.get(jid)})
    return con, chats, lid_of


def android_rows(con, chat, lid_of, skip):
    q = f"""SELECT m._id, m.from_me, m.key_id, sj.raw_string, m.timestamp, m.message_type, m.text_data, m.starred,
                   mm.file_path, mm.width, mm.height, mm.media_duration, mm.media_name, mm.file_hash, mm.mime_type,
                   ml.latitude, ml.longitude, ml.place_name,
                   (SELECT vcard FROM message_vcard v WHERE v.message_row_id = m._id ORDER BY v._id LIMIT 1),
                   q.key_id, q.from_me, qj.raw_string, q.text_data
            FROM message m LEFT JOIN jid sj ON sj._id = m.sender_jid_row_id
            LEFT JOIN message_media mm ON mm.message_row_id = m._id
            LEFT JOIN message_location ml ON ml.message_row_id = m._id
            LEFT JOIN message_quoted q ON q.message_row_id = m._id LEFT JOIN jid qj ON qj._id = q.sender_jid_row_id
            WHERE m.chat_row_id = ? ORDER BY m.timestamp, m._id"""
    rows = []
    for (mid, from_me, key, sender, ts, mtype, text, starred, path, width, height, dur, mname, fhash, mime,
         lat, lon, place, vcard, qkey, qme, qsender, qtext) in con.execute(q, (chat["id"],)):
        if mtype not in TYPES:
            skip("system/call/poll/other")
            continue
        if not ts or ts <= 0:
            skip("no date")
            continue
        text = (text or "").strip() or None
        r = {"ts": ts / 1000, "from_me": int(bool(from_me)), "type": mtype, "text": text, "key": key,
             "starred": bool(starred), "sender": None, "media": None, "quote": None}
        if chat["group"] and not from_me:
            if not sender:
                skip("group message without sender")
                continue
            r["sender"] = lid_of.get(sender, sender)
        if mtype == 0 and not text:
            skip("empty")
            continue
        if mtype == 5:
            if not lat and not lon:
                skip("empty")
                continue
            r.update(lat=lat, lon=lon, place=place)
        if mtype == 4:
            if not vcard:
                skip("contact without card")
                continue
            fn = re.search(r"^FN[;:](.*)$", vcard, re.M)
            r.update(vcard=vcard, text=None, vname=text or (fn.group(1).strip() if fn else "Contact"))
        if mtype in MEDIA:
            r["media"] = {"path": path, "width": width or 0, "height": height or 0, "dur": dur or 0,
                          "name": mname, "hash": fhash, "mime": mime}
        if qkey:
            r["quote"] = {"key": qkey, "me": bool(qme), "sender": lid_of.get(qsender, qsender), "text": qtext}
        rows.append(r)
    return rows


def find_media(media_root, path):
    """message_media.file_path (relative "Media/..." or an absolute /storage/... path) -> file on this Mac"""
    if not path:
        return None
    p = path.replace("\\", "/")
    m = re.search(r"(?:^|/)(Media/.*)$", p)
    full = os.path.join(media_root, m.group(1) if m else p.lstrip("/"))
    return full if os.path.isfile(full) else None


# --- the iPhone side ---
def file_id(rel):
    return hashlib.sha1(f"{DOMAIN}-{rel}".encode()).hexdigest()


class Backup:
    def __init__(self, root):
        self.root = root
        mdb = os.path.join(root, "Manifest.db")
        if not os.path.exists(mdb):
            die(f"no Manifest.db in {root} - point --backup at the backup folder itself")
        info = ios_backup.read_plist(os.path.join(root, "Manifest.plist"))
        if info.get("IsEncrypted"):
            die("the backup is encrypted - make an unencrypted one (Finder: untick 'Encrypt local backup')")
        self.man = sqlite3.connect(mdb)
        row = self.man.execute("SELECT fileID, file FROM Files WHERE domain=? AND relativePath='ChatStorage.sqlite'",
                               (DOMAIN,)).fetchone()
        if not row:
            die("the backup has no WhatsApp ChatStorage.sqlite (is WhatsApp installed and backed up?)")
        self.chat_id, self.template = row
        self.chat_path = self.path(self.chat_id)
        self.known = {p for (p,) in self.man.execute("SELECT relativePath FROM Files WHERE domain=?", (DOMAIN,))}
        self.inode = (self.man.execute("SELECT count(*) FROM Files").fetchone()[0] + 1) * 1000 + 50000000

    def path(self, fid):
        return os.path.join(self.root, fid[:2], fid)

    def record(self, rel, size, is_dir):
        """an MBFile keyed archive like the ones iOS writes, from the ChatStorage.sqlite record"""
        arch = plistlib.loads(self.template)
        objs = arch["$objects"]
        attrs = next(o for o in objs if isinstance(o, dict) and "Mode" in o)
        cls = next(o for o in objs if isinstance(o, dict) and o.get("$classname") == "MBFile")
        now = int(time.time())
        self.inode += 1
        attrs = {k: v for k, v in attrs.items() if k not in ("ExtendedAttributes", "RelativePath", "$class")}
        attrs.update(LastModified=now, LastStatusChange=now, Birth=now, Size=0 if is_dir else size,
                     InodeNumber=self.inode, Mode=0o40755 if is_dir else 0o100644, Flags=0,
                     ProtectionClass=0 if is_dir else 3, RelativePath=plistlib.UID(2), **{"$class": plistlib.UID(3)})
        arch["$objects"] = ["$null", attrs, rel, cls]
        return plistlib.dumps(arch, fmt=plistlib.FMT_BINARY)

    def add_dir(self, rel):
        if rel in self.known:
            return
        parent = rel.rsplit("/", 1)[0]
        if "/" in rel:
            self.add_dir(parent)
        self.man.execute("INSERT OR REPLACE INTO Files VALUES(?,?,?,2,?)",
                         (file_id(rel), DOMAIN, rel, self.record(rel, 0, True)))
        self.known.add(rel)

    def add_file(self, rel, src):
        self.add_dir(rel.rsplit("/", 1)[0])
        fid = file_id(rel)
        dst = self.path(fid)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)  # same disk: no extra space
        except OSError:
            shutil.copyfile(src, dst)
        self.man.execute("INSERT OR REPLACE INTO Files VALUES(?,?,?,1,?)",
                         (fid, DOMAIN, rel, self.record(rel, os.path.getsize(src), False)))
        self.known.add(rel)

    def replace_chatstorage(self, src):
        shutil.copyfile(src, self.chat_path)
        arch = plistlib.loads(self.template)
        attrs = next(o for o in arch["$objects"] if isinstance(o, dict) and "Mode" in o)
        attrs.update(Size=os.path.getsize(src), LastModified=int(time.time()))
        self.man.execute("UPDATE Files SET file=? WHERE fileID=?",
                         (plistlib.dumps(arch, fmt=plistlib.FMT_BINARY), self.chat_id))


def my_jid(ios):
    """own @lid jid, from the blobs of sent messages (field 3)"""
    counts = {}
    for (blob,) in ios.execute("SELECT ZPUSHNAME FROM ZWAMESSAGE WHERE ZISFROMME=1 AND ZPUSHNAME IS NOT NULL "
                               "ORDER BY Z_PK DESC LIMIT 200"):
        m = re.search(rb"\x1a.([0-9]+@lid)", base64.b64decode(blob))
        if m:
            counts[m.group(1).decode()] = counts.get(m.group(1).decode(), 0) + 1
    return max(counts, key=counts.get) if counts else None


def drop_existing(ios, session, rows):
    """drop rows the iPhone already has: same id, or same sender + text (2 min, 15 min for 6+ characters) or the
    same media type (61 s), matched one-to-one to the closest"""
    have, keys = {}, set()
    for key, me, t, text, date in ios.execute(
            "SELECT ZSTANZAID, ZISFROMME, ZMESSAGETYPE, ZTEXT, ZMESSAGEDATE FROM ZWAMESSAGE WHERE ZCHATSESSION=?",
            (session,)):
        if key:
            keys.add(key)
        kind = BACK.get(t)
        if kind is None or not date:
            continue
        text = (text or "").strip() or None
        sig = (int(bool(me)), kind, text if kind == 0 else None)
        have.setdefault(sig, []).append([date + APPLE_EPOCH, False])
    out = []
    for r in rows:
        if r["key"] in keys:
            continue
        sig = (r["from_me"], r["type"], r["text"] if r["type"] == 0 else None)
        window = (900 if len(r["text"] or "") >= 6 else 120) if r["type"] == 0 else 61
        best = None
        for c in have.get(sig, ()):
            d = abs(c[0] - r["ts"])
            if not c[1] and d <= window and (best is None or d < abs(best[0] - r["ts"])):
                best = c
        if best:
            best[1] = True
            continue
        out.append(r)
    return out


class Writer:
    def __init__(self, ios, me, backup, media_root):
        self.ios, self.me, self.backup, self.media_root = ios, me, backup, media_root
        self.pk = dict(ios.execute("SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY"))
        self.docid = ios.execute("SELECT coalesce(max(ZDOCID), 0) FROM ZWAMESSAGE").fetchone()[0]
        self.members = {(s, j): pk for pk, s, j in ios.execute(
            "SELECT Z_PK, ZCHATSESSION, ZMEMBERJID FROM ZWAGROUPMEMBER")}
        self.files, self.thumbs, self.missing = [], [], 0

    def next_pk(self, entity):
        self.pk[entity] += 1
        return self.pk[entity]

    def session(self, chat, first_ts):
        pk = self.next_pk("WAChatSession")
        group_info = None
        if chat["group"]:
            group_info = self.next_pk("WAGroupInfo")
            self.ios.execute("INSERT INTO ZWAGROUPINFO(Z_PK, Z_ENT, Z_OPT, ZSTATE, ZCHATSESSION, ZCREATIONDATE) "
                             "VALUES(?,5,1,0,?,?)", (group_info, pk, first_ts - APPLE_EPOCH))
        self.ios.execute(
            """INSERT INTO ZWACHATSESSION(Z_PK, Z_ENT, Z_OPT, ZARCHIVED, ZCONTACTABID, ZFLAGS, ZHIDDEN,
                   ZIDENTITYVERIFICATIONEPOCH, ZIDENTITYVERIFICATIONSTATE, ZMESSAGECOUNTER, ZREMOVED, ZSESSIONTYPE,
                   ZSPOTLIGHTSTATUS, ZUNREADCOUNT, ZGROUPINFO, ZCONTACTIDENTIFIER, ZCONTACTJID, ZPARTNERNAME, ZETAG)
               VALUES(?,4,1,?,0,272,?,2147483647,0,1,0,?,?,0,?,?,?,?,?)""",
            (pk, chat["archived"], chat["hidden"], 1 if chat["group"] else 0, 1 if chat["group"] else -5,
             group_info, chat["pn"], chat["jid"], chat["name"] or (chat["pn"] or chat["jid"]).split("@")[0],
             "lid" if chat["group"] else None))
        return pk

    def member(self, session, jid):
        hit = self.members.get((session, jid))
        if hit is None:
            hit = self.next_pk("WAGroupMember")
            self.ios.execute("INSERT INTO ZWAGROUPMEMBER(Z_PK, Z_ENT, Z_OPT, ZCONTACTABID, ZISACTIVE, ZISADMIN, "
                             "ZCHATSESSION, ZMEMBERJID) VALUES(?,6,1,0,0,0,?,?)", (hit, session, jid))
            self.members[(session, jid)] = hit
        return hit

    def media_item(self, chat, r, msg_pk):
        """the ZWAMEDIAITEM row every message has; for media it points at the file copied into the backup"""
        pk = self.next_pk("WAMediaItem")
        v = {"ZFILESIZE": 0, "ZMOVIEDURATION": 0, "ZLATITUDE": 0.0, "ZLONGITUDE": 0.0, "ZMEDIAORIGIN": 0,
             "ZMETADATA": None}
        if r["quote"]:
            q = dict(r["quote"], sender=self.me if r["quote"]["me"] else r["quote"]["sender"] or chat["jid"])
            v["ZMETADATA"] = quote_blob(q)
        if r["type"] == 5:
            v.update(ZLATITUDE=r["lat"], ZLONGITUDE=r["lon"], ZTITLE=r.get("place"))
        elif r["type"] == 4:
            v.update(ZVCARDNAME=r["vname"], ZVCARDSTRING=r["vcard"])
        elif r["media"]:
            md = r["media"]
            src = md["src"]
            ext = os.path.splitext(src)[1].lower() or ".bin"
            u = str(uuid.uuid4())
            local = f"Media/{chat['jid']}/{u[0]}/{u[1]}/{u}{ext}"
            self.files.append((f"Message/{local}", src))
            v.update(ZMEDIALOCALPATH=local, ZFILESIZE=os.path.getsize(src), ZMOVIEDURATION=int(md["dur"] or 0),
                     ZLATITUDE=float(md["height"] or 0), ZLONGITUDE=float(md["width"] or 0),
                     ZVCARDNAME=md["hash"] or base64.b64encode(hashlib.sha256(open(src, "rb").read()).digest()).decode())
            if r["type"] == 2 and ext in (".opus", ".ogg"):
                v["ZMEDIAORIGIN"] = 1  # voice note
            if r["type"] == 9:
                name = md["name"] or os.path.basename(src)
                v.update(ZAUTHORNAME=name, ZTITLE=os.path.splitext(name)[0])
            if r["type"] in (1, 3, 13):
                thumb = f"Media/{chat['jid']}/{u[0]}/{u[1]}/{u}.thumb"
                v["ZXMPPTHUMBPATH"] = thumb
                self.thumbs.append((r["type"] == 1, src, f"Message/{thumb}"))
        cols = ["Z_PK", "Z_ENT", "Z_OPT", "ZMESSAGE", "ZASPECTRATIO", "ZHACCURACY", "ZCLOUDSTATUS"] + list(v)
        self.ios.execute(f"INSERT INTO ZWAMEDIAITEM({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                         [pk, 8, 1, msg_pk, 0.0, 0.0, 0] + list(v.values()))
        return pk

    def message(self, chat, session, r):
        pk = self.next_pk("WAMessage")
        self.docid += 1
        ios_type = TYPES[r["type"]]
        media_pk = self.media_item(chat, r, pk)
        date = r["ts"] - APPLE_EPOCH
        section = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m") if ios_type in SECTION else None
        sent = r["from_me"]
        member = self.member(session, r["sender"]) if r["sender"] else None
        text = None if r["type"] in (4, 5, 15) else r["text"]
        self.ios.execute(
            """INSERT INTO ZWAMESSAGE(Z_PK, Z_ENT, Z_OPT, ZCHILDMESSAGESDELIVEREDCOUNT, ZCHILDMESSAGESPLAYEDCOUNT,
                   ZCHILDMESSAGESREADCOUNT, ZDATAITEMVERSION, ZDOCID, ZENCRETRYCOUNT, ZFILTEREDRECIPIENTCOUNT, ZFLAGS,
                   ZGROUPEVENTTYPE, ZISFROMME, ZMESSAGEERRORSTATUS, ZMESSAGESTATUS, ZMESSAGETYPE, ZSORT,
                   ZSPOTLIGHTSTATUS, ZSTARRED, ZCHATSESSION, ZGROUPMEMBER, ZMEDIAITEM, ZMESSAGEDATE, ZSENTDATE,
                   ZFROMJID, ZMEDIASECTIONID, ZPUSHNAME, ZSTANZAID, ZTEXT, ZTOJID)
               VALUES(?,9,1,0,0,0,3,?,0,0,?,?,?,0,?,?,0,-32768,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pk, self.docid, 16777280 if sent else 16777216, 0 if sent else 2, sent, 6 if sent else 8, ios_type,
             1 if r["starred"] else None, session, member, media_pk, date, date,
             None if sent else chat["jid"], section, push_blob(sent, r["ts"], self.me),
             r["key"] or uuid.uuid4().hex[:20].upper(), text, chat["jid"] if sent else None))
        if text:
            self.ios.execute("INSERT INTO ZWAMESSAGE_fts(_id, content) VALUES(?,?)", (pk, text))

    def finish_session(self, session):
        """number the chat's messages by time, point the chat at its last message"""
        order = self.ios.execute("SELECT Z_PK, ZSORT FROM ZWAMESSAGE WHERE ZCHATSESSION=? "
                                 "ORDER BY ZMESSAGEDATE, ZSORT, Z_PK", (session,)).fetchall()
        self.ios.executemany("UPDATE ZWAMESSAGE SET ZSORT=? WHERE Z_PK=?",
                             [(i, pk) for i, (pk, s) in enumerate(order, 1) if s != i])
        last = order[-1][0]
        self.ios.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION=NULL WHERE ZLASTSESSION=? AND Z_PK!=?", (session, last))
        self.ios.execute("UPDATE ZWAMESSAGE SET ZLASTSESSION=? WHERE Z_PK=?", (session, last))
        date, text = self.ios.execute("SELECT ZMESSAGEDATE, ZTEXT FROM ZWAMESSAGE WHERE Z_PK=?", (last,)).fetchone()
        old = self.ios.execute("SELECT ZLASTMESSAGE FROM ZWACHATSESSION WHERE Z_PK=?", (session,)).fetchone()[0]
        # ZLASTMESSAGETEXT is the app's own blob for the last message: keep it if that message did not change
        self.ios.execute("UPDATE ZWACHATSESSION SET ZMESSAGECOUNTER=?, ZLASTMESSAGE=?, ZLASTMESSAGEDATE=?, "
                         "ZLASTMESSAGETEXT=CASE WHEN ZLASTMESSAGE=? THEN ZLASTMESSAGETEXT END WHERE Z_PK=?",
                         (len(order) + 1, last, date, last, session))

    def save_pks(self):
        self.ios.executemany("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME=?", [(v, k) for k, v in self.pk.items()])


def make_thumbs(thumbs, out_dir):
    """{backup path: thumbnail jpeg}: photos with sips, videos/GIFs with Quick Look (qlmanage)"""
    made = {}
    # macOS tools; elsewhere the app shows the photo itself and simply has no preview thumbnail
    photos = [(s, rel) for img, s, rel in thumbs if img] if shutil.which("sips") else []
    videos = [(s, rel) for img, s, rel in thumbs if not img] if shutil.which("qlmanage") and shutil.which("sips") else []
    for i in range(0, len(photos), 200):
        d = os.path.join(out_dir, f"p{i}")
        os.makedirs(os.path.join(d, "out"))
        names = []
        for j, (s, rel) in enumerate(photos[i:i + 200]):
            names.append(os.path.join(d, f"{j}{os.path.splitext(s)[1]}"))
            try:
                os.link(s, names[-1])  # not a symlink: sips names its output after the link's target
            except OSError:
                shutil.copyfile(s, names[-1])
        subprocess.run(["sips", "-Z", "100", "-s", "format", "jpeg", "-s", "formatOptions", "60", *names,
                        "--out", os.path.join(d, "out")], capture_output=True)
        for name, (s, rel) in zip(names, photos[i:i + 200]):
            t = os.path.join(d, "out", os.path.basename(name))  # sips keeps the input's name
            if os.path.exists(t):
                made[rel] = t
    for i in range(0, len(videos), 50):
        batch = videos[i:i + 50]
        d = os.path.join(out_dir, f"v{i}")
        os.makedirs(d)
        subprocess.run(["qlmanage", "-t", "-s", "200", "-o", d, *[s for s, _ in batch]], capture_output=True)
        for s, rel in batch:
            png = os.path.join(d, os.path.basename(s) + ".png")
            jpg = png[:-4] + ".thumb.jpg"
            if os.path.exists(png):
                subprocess.run(["sips", "-s", "format", "jpeg", png, "--out", jpg], capture_output=True)
                if os.path.exists(jpg):
                    made[rel] = jpg
    return made


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Android msgstore.db")
    p.add_argument("--wa-db", help="Android wa.db, for names of new chats (default: next to msgstore.db)")
    p.add_argument("--backup", required=True, help="copy of the unencrypted iPhone backup folder (has Manifest.db)")
    p.add_argument("--media", help="copy of the phone's WhatsApp folder (holding Media/), for photos, voice notes...")
    p.add_argument("--apply", action="store_true", help="actually write into the backup (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not os.path.exists(a.db):
        die(f"{a.db} not found")
    if a.media and not os.path.isdir(os.path.join(a.media, "Media")):
        die(f"no Media folder in {a.media}")
    backup = Backup(a.backup)
    con, chats, lid_of = read_android(a.db, a.wa_db or os.path.join(os.path.dirname(os.path.abspath(a.db)), "wa.db"))

    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "ChatStorage.sqlite")
        shutil.copyfile(backup.chat_path, work)
        ios = sqlite3.connect(work)
        ios.execute("BEGIN")
        me = my_jid(ios)
        if not me:
            die("could not find your own jid in the iPhone's sent messages")
        sessions = {j: pk for pk, j in ios.execute(
            "SELECT Z_PK, ZCONTACTJID FROM ZWACHATSESSION WHERE ZSESSIONTYPE IN (0, 1) AND ZCONTACTJID IS NOT NULL")}
        by_pn = {j: pk for pk, j in ios.execute(
            "SELECT Z_PK, ZCONTACTIDENTIFIER FROM ZWACHATSESSION WHERE ZSESSIONTYPE=0 AND ZCONTACTIDENTIFIER IS NOT NULL")}
        wr = Writer(ios, me, backup, a.media)
        skipped, counts, report, already, created = {}, {}, [], 0, 0
        skip = lambda why: skipped.__setitem__(why, skipped.get(why, 0) + 1)
        for chat in chats:
            rows = android_rows(con, chat, lid_of, skip)
            for r in rows:
                if r["media"]:
                    src = a.media and find_media(a.media, r["media"]["path"])
                    if src:
                        r["media"]["src"] = src
                        continue
                    skip("media file missing")
                    if r["text"] and r["type"] in (1, 3, 13):
                        r.update(type=0, media=None)  # keep the caption
                    else:
                        r["drop"] = True
            rows = [r for r in rows if not r.get("drop")]
            if not rows:
                continue
            session = sessions.get(chat["jid"]) or (chat["pn"] and by_pn.get(chat["pn"]))
            new = drop_existing(ios, session, rows) if session else rows
            already += len(rows) - len(new)
            if not new:
                continue
            if not session:
                session = wr.session(chat, new[0]["ts"])
                sessions[chat["jid"]] = session
                created += 1
            for r in new:
                wr.message(chat, session, r)
                counts[KINDS[r["type"]]] = counts.get(KINDS[r["type"]], 0) + 1
            wr.finish_session(session)
            report.append((chat["name"] or chat["jid"], len(new)))
        wr.save_pks()
        total = sum(counts.values())
        print(f"iphone: {me}, {len(sessions)} chats")
        print("skip:   " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])))
        print(f"skip:   {already} already on the iPhone")
        print(f"add:    {total} messages into {len(report)} chats ({created} new): "
              + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
        for name, n in sorted(report, key=lambda x: -x[1])[:25]:
            print(f"        {n:6} {name}")
        if len(report) > 25:
            print(f"        ... and {len(report) - 25} more chats")
        print(f"media:  {len(wr.files)} files, {sum(os.path.getsize(s) for _, s in wr.files) / 1e9:.2f} GB")
        ok = ios.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            die(f"integrity check failed: {ok}")
        if not a.apply:
            ios.rollback()
            print("\ndry run - nothing written. Re-run with --apply to write into the backup.")
            return
        print(f"thumbs: making {len(wr.thumbs)} thumbnails ...")
        thumbs = make_thumbs(wr.thumbs, os.path.join(tmp, "thumbs"))
        # a thumbnail path with no file behind it: leave the preview to the app
        ios.executemany("UPDATE ZWAMEDIAITEM SET ZXMPPTHUMBPATH=NULL WHERE ZXMPPTHUMBPATH=?",
                        [(rel[len("Message/"):],) for _, _, rel in wr.thumbs if rel not in thumbs])
        ios.commit()
        ios.execute("PRAGMA journal_mode=DELETE")
        ios.close()

        backup.man.execute("BEGIN")
        for i, (rel, src) in enumerate(wr.files, 1):
            backup.add_file(rel, src)
            if i % 2000 == 0:
                print(f"        {i}/{len(wr.files)} files")
        for rel, src in thumbs.items():
            backup.add_file(rel, src)  # a hard link outlives the temp folder
        backup.replace_chatstorage(work)
        backup.man.commit()
        backup.man.close()
    missing, wrong = ios_backup.check(a.backup, fix=True)
    if missing or wrong:
        print(f"fixed:  {len(missing)} missing files and {len(wrong)} bad records the backup came with "
              f"(they would make the restore fail)")
    print(f"done:   {total} messages, {len(wr.files)} media files, {len(thumbs)} thumbnails written to {a.backup}")
    print(f"next:   wakit ios-backup restore --backup '{a.backup}' --yes   (replaces everything on the iPhone "
          f"with this backup - keep the original backup safe)")


if __name__ == "__main__":
    main()
