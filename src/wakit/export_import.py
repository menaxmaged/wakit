"""Import a WhatsApp "Export chat" file into an Android msgstore.db as chat history.

Works with iPhone exports ("[dd/mm/yyyy, h:mm:ss AM] Name: text") and Android exports
("dd/mm/yyyy, hh:mm - Name: text"), as a .zip, a folder, or a bare _chat.txt.

Messages already in the chat (same sender, same text or media type, within a few seconds) are
skipped, so re-running on the same export (or an export that overlaps the database) never
duplicates. Imported messages are slotted between the chat's existing messages by time.

Dry run by default; pass --apply to write. A timestamped backup is made before writing.

examples:
  wakit import --db msgstore.db --export "WhatsApp Chat - Alex.zip" --contact 15551234567
  wakit import --db msgstore.db --export chat.zip --contact "Alex" --me "Sam" --apply
"""
import argparse, base64, hashlib, mimetypes, os, re, secrets, shutil, sqlite3, subprocess, sys, tempfile, unicodedata, zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

LRM = "‎"
IOS_HDR = re.compile(r"^‎?\[(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?[Mm]\.?)?)\] ([^:]+?): ?(.*)$")
AND_HDR = re.compile(r"^‎?(\d{1,2}/\d{1,2}/\d{2,4}),? (\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap]\.?[Mm]\.?)?) - ([^:]+?): ?(.*)$")
# a dated line with no "Name:" - a system line (Android "date, time - You blocked this person.", iOS without sender)
DATED = re.compile(r"^‎?(\[?\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[APap]\.?[Mm]\.?)?\]? (- )?)")
IOS_ATTACH = re.compile(r"\s*‎?<attached: ([^>]+)>\s*$")
AND_ATTACH = re.compile(r"^‎?([^\n]+?\.\w{0,5}) \(file attached\)\s*", re.S)  # "AUD-...-WA0003." happens too
SYSTEM = re.compile(r"end-to-end encrypted|disappearing messages|security code|^(Missed )?(voice|video) call|"
                    r"changed (their|your) phone number|^You (blocked|unblocked) this (contact|person)|^\S+ is a contact\.$|^Waiting for this message|"
                    r"advanced chat privacy|^Call failed|created the username", re.I)
OMITTED = re.compile(r"^(<[^<>\n]+ omitted>|(Media|image|video|audio|sticker|GIF|document) omitted)$", re.I)
OMITTED_TAIL = re.compile(r"^(.*?)\s*(image|video|audio|sticker|GIF|document|Contact card) omitted$", re.S)  # "caption ‎image omitted"
DELETED = {"You deleted this message.", "This message was deleted.", "You deleted this message", "This message was deleted"}
EDITED = re.compile(r"\s*‎?<This message was edited>")

# extension -> (message_type, folder under Media/)
MEDIA_TYPES = {
    **dict.fromkeys(["jpg", "jpeg", "png", "heic"], (1, "WhatsApp Images")),
    **dict.fromkeys(["opus", "ogg", "m4a", "mp3", "aac", "amr", "wav"], (2, "WhatsApp Audio")),
    **dict.fromkeys(["mp4", "mov", "3gp", "mkv"], (3, "WhatsApp Video")),
    "webp": (20, "WhatsApp Stickers"),
    "gif": (13, "WhatsApp Animated Gifs"),
}
DOCUMENT = (9, "WhatsApp Documents")
MIME = {"opus": "audio/ogg; codecs=opus", "ogg": "audio/ogg; codecs=opus", "webp": "image/webp", "heic": "image/heic"}


# leading bytes -> extension, for attachments exported without one
MAGIC = [(b"%PDF", "pdf"), (b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"OggS", "opus"), (b"ID3", "mp3"),
         (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"), (b"RIFF", "webp"), (b"GIF8", "gif")]


def sniff_ext(path):
    head = open(path, "rb").read(12)
    if head[4:8] == b"ftyp":
        return "mp4"
    return next((ext for magic, ext in MAGIC if head.startswith(magic)), "")


def die(msg):
    sys.exit(f"error: {msg}")


# ---------------------------------------------------------------- export reading

def open_export(path, workdir):
    """Return (chat_txt_path, media_dir) for a zip, folder, or txt file."""
    if zipfile.is_zipfile(path):
        out = os.path.join(workdir, "export")
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():  # flat extract; refuse path traversal
                if n.endswith("/"):
                    continue
                name = os.path.basename(n)
                if name:
                    os.makedirs(out, exist_ok=True)
                    with z.open(n) as src, open(os.path.join(out, name), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        path = out
    if os.path.isdir(path):
        txts = [f for f in os.listdir(path) if f.endswith(".txt")]
        if not txts:
            die(f"no .txt chat file in {path}")
        txt = "_chat.txt" if "_chat.txt" in txts else sorted(txts)[0]
        return os.path.join(path, txt), path
    return path, os.path.dirname(os.path.abspath(path))


def parse_lines(txt_path):
    text = open(txt_path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    lines = text.split("\n")
    hdr = IOS_HDR if sum(bool(IOS_HDR.match(l)) for l in lines[:50]) >= sum(bool(AND_HDR.match(l)) for l in lines[:50]) else AND_HDR
    msgs, in_system = [], False
    for line in lines:
        m = hdr.match(line)
        if m:
            d, t, sender, body = m.groups()
            msgs.append({"date": d, "time": t, "sender": sender.strip(), "body": body, "line": line})
            in_system = False
        elif DATED.match(line):
            in_system = True  # system line without a sender: not part of the message before it
        elif msgs and not in_system:
            msgs[-1]["body"] += "\n" + line
        # lines before the first header, and system lines without "Name:", are ignored
    counts = {}
    for m in msgs:
        counts[m["sender"]] = counts.get(m["sender"], 0) + 1
    if len(counts) > 2:
        # a line inside a message that starts like "[date, time] Something: ..." (pasted logs etc.) parses as a
        # message from an extra sender; fold such rare senders back into the message before them
        stray = {s for s, n in counts.items() if n <= max(3, len(msgs) // 10000)}
        if stray and len(counts) - len(stray) <= 2:
            print(f"note: treating {sum(counts[s] for s in stray)} lines with stray senders {sorted(stray)} as message text")
            out = []
            for m in msgs:
                if m["sender"] in stray and out:
                    out[-1]["body"] += "\n" + m["line"]
                else:
                    out.append(m)
            msgs = out
    return msgs, ("ios" if hdr is IOS_HDR else "android")


def detect_date_order(msgs, forced):
    if forced != "auto":
        return forced
    a = b = 0
    for m in msgs:
        x, y, _ = m["date"].split("/")
        a, b = max(a, int(x)), max(b, int(y))
    if a > 12:
        return "dmy"
    if b > 12:
        return "mdy"
    print("warning: could not tell day/month order from the dates, assuming day/month (use --date-order)")
    return "dmy"


def to_ms(m, order, tz):
    d1, d2, y = m["date"].split("/")
    day, mon = (d1, d2) if order == "dmy" else (d2, d1)
    year = int(y) + (2000 if len(y) == 2 else 0)
    t = re.sub(r"\s", "", m["time"]).replace(".", ":").upper().replace("A:M:", "AM").replace("P:M:", "PM")
    t = t.replace("A:M", "AM").replace("P:M", "PM")
    ampm = t[-2:] if t[-2:] in ("AM", "PM") else None
    hms = (t[:-2] if ampm else t).split(":")
    h, mi, s = int(hms[0]), int(hms[1]), int(hms[2]) if len(hms) > 2 else 0
    if ampm:
        h = h % 12 + (12 if ampm == "PM" else 0)
    return int(datetime(year, int(mon), int(day), h, mi, s, tzinfo=tz).timestamp() * 1000)


def classify(msgs, fmt, me, order, tz, media_dir):
    rows, skipped = [], {"system": 0, "empty": 0, "media omitted": 0, "media file missing": 0}
    for m in msgs:
        body = EDITED.sub("", m["body"]).rstrip()
        plain = body.replace(LRM, "").strip()
        if not plain:
            skipped["empty"] += 1
            continue
        if SYSTEM.search(plain) and (body.startswith(LRM) or fmt == "android"):
            skipped["system"] += 1
            continue
        if OMITTED.match(plain):
            skipped["media omitted"] += 1
            continue
        first, _, rest = plain.partition("\n")
        if rest.strip() and OMITTED.match(first.strip()):
            # Android: "<Media omitted>" with the caption on the next line - keep the caption as text
            skipped["media omitted"] += 1
            plain = body = rest.strip()
        tail = OMITTED_TAIL.match(plain)
        if tail:
            # media left out of the export: keep a caption as text (a document's "name • N pages" is not one)
            skipped["media omitted"] += 1
            if tail.group(2) == "document" or not tail.group(1).strip():
                continue
            body = tail.group(1).strip()
        r = {"ts": to_ms(m, order, tz), "from_me": int(m["sender"] == me), "type": 0, "text": body, "media": None}
        if plain in DELETED:
            r.update(type=15, text=None)
        else:
            a = IOS_ATTACH.search(body) if fmt == "ios" else AND_ATTACH.match(body)
            if a:
                fname = a.group(1)
                caption = (body[: a.start()] if fmt == "ios" else body[a.end():]).replace(LRM, "").strip() or None
                fpath = os.path.join(media_dir, fname)
                for form in ("NFC", "NFD"):  # macOS/zip may store the name in the other Unicode form
                    alt = os.path.join(media_dir, unicodedata.normalize(form, fname))
                    if not os.path.isfile(fpath) and os.path.isfile(alt):
                        fpath = alt
                if not os.path.isfile(fpath):
                    skipped["media file missing"] += 1
                    if not caption:
                        continue
                    r.update(text=caption)
                elif fname.lower().endswith(".vcf"):
                    # contact card: shown by the app from message_vcard, with the contact's name as the text
                    card = open(fpath, encoding="utf-8", errors="replace").read()
                    name = re.search(r"^FN[;:](.*)$", card, re.M)
                    r.update(type=4, text=(name.group(1).strip() if name else fname[:-4]), vcard=card)
                else:
                    ext = fname.rsplit(".", 1)[-1].lower()
                    name = fname
                    if not ext:  # exported without an extension: tell the type from the file, and name it with one
                        ext = sniff_ext(fpath)
                        name = fname + ext if ext else fname.rstrip(".")
                    mtype, folder = MEDIA_TYPES.get(ext, DOCUMENT)
                    if "-AUDIO-" in fname or fname.startswith("PTT-"):
                        folder = "WhatsApp Voice Notes"
                    mime = MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
                    r.update(type=mtype, text=caption, media={"name": name, "path": fpath, "mime": mime, "folder": folder})
        rows.append(r)
    return rows, skipped


# ---------------------------------------------------------------- database

def find_chat(con, contact, wa_db):
    """Return (chat_row_id or None, phone_jid_user) for a phone number or contact name."""
    number = re.sub(r"[^\d]", "", contact)
    if not number or len(number) < 7:
        if not wa_db or not os.path.exists(wa_db):
            die(f"'{contact}' is not a phone number and no wa.db found to look up names (use --wa-db)")
        w = sqlite3.connect(f"file:{wa_db}?mode=ro", uri=True)
        hits = w.execute("SELECT DISTINCT number, display_name FROM wa_contacts WHERE number IS NOT NULL AND "
                         "(display_name LIKE ? OR wa_name LIKE ? OR given_name LIKE ?)", (f"%{contact}%",) * 3).fetchall()
        w.close()
        if len(hits) != 1:
            die(f"contact name '{contact}' matched {len(hits)} contacts: {hits[:10]} - pass the phone number instead")
        number = hits[0][0]
        print(f"contact: {hits[0][1]} ({number})")
    row = con.execute(
        """SELECT c._id FROM chat c WHERE c.jid_row_id IN (
               SELECT _id FROM jid WHERE user=? AND server='s.whatsapp.net' AND device=0
               UNION SELECT lid_row_id FROM jid_map WHERE jid_row_id IN
                   (SELECT _id FROM jid WHERE user=? AND server='s.whatsapp.net'))""", (number, number)).fetchall()
    if len(row) > 1:
        print(f"warning: number has {len(row)} chats ({[r[0] for r in row]}), using the one with most messages")
        row = [con.execute(f"SELECT chat_row_id FROM message WHERE chat_row_id IN ({','.join(str(r[0]) for r in row)}) "
                           "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1").fetchone() or row[0]]
    return (row[0][0] if row else None), number


def create_chat(cur, number, first_ts, last_ts):
    raw = f"{number}@s.whatsapp.net"
    jid = cur.execute("SELECT _id FROM jid WHERE raw_string=?", (raw,)).fetchone()
    jid_id = jid[0] if jid else cur.execute(
        "INSERT INTO jid(user, server, agent, device, type, raw_string) VALUES(?, 's.whatsapp.net', 0, 0, 0, ?)",
        (number, raw)).lastrowid
    return cur.execute(
        """INSERT INTO chat(jid_row_id, hidden, created_timestamp, display_message_row_id, last_message_row_id,
               last_read_message_row_id, last_read_receipt_sent_message_row_id, last_important_message_row_id, archived,
               sort_timestamp, spam_detection, unseen_earliest_message_received_time, unseen_message_count,
               unseen_missed_calls_count, unseen_row_count, plaintext_disabled, vcard_ui_dismissed,
               change_number_notified_message_row_id, show_group_description, ephemeral_expiration,
               ephemeral_setting_timestamp, ephemeral_disappearing_messages_initiator, group_type, chat_origin,
               chat_lock, participation_status, account_jid_row_id, group_member_count, limited_sharing,
               limited_sharing_setting_timestamp, ephemeral_after_read_duration, business_chat_state)
           VALUES(?,0,?,1,1,1,0,1,0,?,1,0,0,0,0,1,-1,-1,0,0,0,0,0,'general',0,0,?,-1,0,0,0,0)""",
        (jid_id, first_ts, last_ts, jid_id)).lastrowid


# ---------------------------------------------------------------- media details

def image_size(path):
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path], capture_output=True, text=True).stdout
        w, h = (int(x) for x in re.findall(r"pixel\w+: (\d+)", out))
        return w, h
    except Exception:
        return 0, 0


def jpeg_thumb(path, tmp):
    """Small JPEG preview like the ones WhatsApp keeps in message_thumbnail (uses macOS sips/qlmanage)."""
    src = path
    try:
        if not path.lower().endswith((".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif")):
            # videos: let Quick Look render a frame
            subprocess.run(["qlmanage", "-t", "-s", "320", "-o", tmp, path], capture_output=True, timeout=30)
            src = os.path.join(tmp, os.path.basename(path) + ".png")
            if not os.path.exists(src):
                return None
        out = os.path.join(tmp, "thumb.jpg")
        subprocess.run(["sips", "-Z", "100", "-s", "format", "jpeg", "-s", "formatOptions", "60", src, "--out", out],
                       capture_output=True, timeout=30)
        return open(out, "rb").read() if os.path.exists(out) else None
    except Exception:
        return None


def ogg_opus_info(path):
    """(duration_seconds, waveform) for an Ogg Opus voice note, without decoding audio.
    The waveform approximates loudness from Opus packet sizes (louder speech -> bigger packets)."""
    data = open(path, "rb").read()
    pos, packets, cur, last_gp = 0, [], 0, 0
    while data[pos:pos + 4] == b"OggS":
        gp = int.from_bytes(data[pos + 6:pos + 14], "little", signed=True)
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        start = body - cur
        for l in lacing:
            cur += l
            if l < 255:
                packets.append((start, cur))
                start, cur = start + cur, 0
        if gp > 0:
            last_gp = gp
        pos = body + sum(lacing)
    if len(packets) < 3 or data[packets[0][0]:packets[0][0] + 8] != b"OpusHead":
        return 0, None
    pre_skip = int.from_bytes(data[packets[0][0] + 10:packets[0][0] + 12], "little")
    duration = max(0, round((last_gp - pre_skip) / 48000))
    sizes = [n for _, n in packets[2:]]
    n = len(sizes)
    buckets = [sizes[i * n // 64:max((i + 1) * n // 64, i * n // 64 + 1)] for i in range(64)]  # >=1 packet each
    means = [sum(b) / len(b) for b in buckets]
    lo, hi = min(sizes), max(means) or 1
    wave = bytes(max(0, min(100, int(100 * (m - lo) / max(hi - lo, 1)))) for m in means)
    return duration, wave


def afinfo_duration(path):
    try:
        out = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=30).stdout
        return round(float(re.search(r"estimated duration: ([\d.]+)", out).group(1)))
    except Exception:
        return 0


def mp4_info(path):
    """(width, height, duration_seconds) from an MP4/MOV's moov atoms."""
    data = open(path, "rb").read()
    found = {"w": 0, "h": 0, "dur": 0}

    def walk(start, end):
        p = start
        while p + 8 <= end:
            size = int.from_bytes(data[p:p + 4], "big")
            kind = data[p + 4:p + 8]
            hdr = 8
            if size == 1:
                size, hdr = int.from_bytes(data[p + 8:p + 16], "big"), 16
            elif size == 0:
                size = end - p
            if size < hdr:
                return
            c = p + hdr
            if kind in (b"moov", b"trak", b"mdia"):
                walk(c, p + size)
            elif kind == b"mvhd":
                if data[c] == 1:
                    ts, du = int.from_bytes(data[c + 20:c + 24], "big"), int.from_bytes(data[c + 24:c + 32], "big")
                else:
                    ts, du = int.from_bytes(data[c + 12:c + 16], "big"), int.from_bytes(data[c + 16:c + 20], "big")
                found["dur"] = round(du / ts) if ts else 0
            elif kind == b"tkhd" and not found["w"]:
                o = c + (88 if data[c] == 1 else 76)
                w, h = int.from_bytes(data[o:o + 4], "big") >> 16, int.from_bytes(data[o + 4:o + 8], "big") >> 16
                if w and h:
                    found["w"], found["h"] = w, h
            p += size

    try:
        walk(0, len(data))
    except Exception:
        pass
    return found["w"], found["h"], found["dur"]


def media_details(mtype, path, tmp):
    """dict of width/height/duration/thumbnail/waveform for a media file."""
    d = {"width": 0, "height": 0, "duration": 0, "thumb": None, "wave": None}
    ext = path.rsplit(".", 1)[-1].lower()
    if mtype in (1, 20):
        d["width"], d["height"] = image_size(path)
    if mtype == 1:
        d["thumb"] = jpeg_thumb(path, tmp)
    elif mtype == 2:
        if ext in ("opus", "ogg"):
            d["duration"], d["wave"] = ogg_opus_info(path)
        if not d["duration"]:
            d["duration"] = afinfo_duration(path)
    elif mtype in (3, 13):
        d["width"], d["height"], d["duration"] = mp4_info(path) if ext != "gif" else (*image_size(path), 0)
        d["thumb"] = jpeg_thumb(path, tmp)
    return d


def write_media_details(cur, rid, d):
    cur.execute("UPDATE message_media SET width=?, height=?, media_duration=? WHERE message_row_id=?",
                (d["width"], d["height"], d["duration"], rid))
    if d["thumb"]:
        cur.execute("INSERT OR REPLACE INTO message_thumbnail(message_row_id, thumbnail) VALUES(?,?)", (rid, d["thumb"]))
    if d["wave"]:
        cur.execute("INSERT OR IGNORE INTO audio_data(message_row_id, waveform) VALUES(?,?)", (rid, d["wave"]))


def sync_existing_media(cur, chat_id, media_dir, tmp, media_out, folders):
    """For media rows already in this chat that came from this export (e.g. an earlier run): fill in missing
    previews/durations, point file_path at the phone's folder names, and copy the file into media_out.
    Returns (rows touched, rows whose details were filled)."""
    rows = cur.execute(
        """SELECT m._id, m.message_type, mm.media_name, mm.file_path,
                  (m.message_type IN (1,3) AND t.message_row_id IS NULL) OR
                  (m.message_type=2 AND (coalesce(mm.media_duration,0)=0 OR a.message_row_id IS NULL)) OR
                  (m.message_type=20 AND coalesce(mm.width,0)=0)
           FROM message m JOIN message_media mm ON mm.message_row_id=m._id
           LEFT JOIN message_thumbnail t ON t.message_row_id=m._id LEFT JOIN audio_data a ON a.message_row_id=m._id
           WHERE m.chat_row_id=? AND mm.media_name IS NOT NULL AND mm.file_path LIKE 'Media/%'""", (chat_id,)).fetchall()
    touched = filled = 0
    for rid, mtype, name, rel, missing in rows:
        path = os.path.join(media_dir, name)
        if not os.path.isfile(path):
            continue
        touched += 1
        if missing:
            write_media_details(cur, rid, media_details(mtype, path, tmp))
            filled += 1
        folder = rel.split("/")[1]
        new_rel = f"Media/{folders.get(folder, folder)}/{name}"
        if new_rel != rel:
            cur.execute("UPDATE message_media SET file_path=? WHERE message_row_id=?", (new_rel, rid))
        if media_out:
            dst = os.path.join(media_out, new_rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
    return touched, filled


# ---------------------------------------------------------------- phone (adb)

def adb(*args, check=True):
    r = subprocess.run(["adb", *args], capture_output=True, text=True)
    if check and r.returncode:
        die(f"adb {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def phone_media_layout(package, root):
    """Find the app's media root on the phone and map our folder names to the ones it actually uses."""
    if not shutil.which("adb"):
        die("--push needs adb (brew install android-platform-tools)")
    if adb("get-state", check=False).strip() != "device":
        die("--push: no phone connected over adb (check `adb devices`, USB debugging on)")
    if not root:
        base = f"/sdcard/Android/media/{package}"
        cands = [l.strip() for l in adb("shell", f"ls -d '{base}'/*/Media 2>/dev/null", check=False).splitlines() if l.strip()]
        if not cands:
            die(f"--push: no Media folder under {base}; pass --phone-root")
        root = cands[0].rsplit("/Media", 1)[0]
    existing = [l.strip() for l in adb("shell", f"ls '{root}/Media' 2>/dev/null", check=False).splitlines() if l.strip()]
    folders = {}
    for _, folder in [*MEDIA_TYPES.values(), DOCUMENT, (0, "WhatsApp Voice Notes")]:
        suffix = folder.split("WhatsApp ", 1)[1]  # "Images", "Voice Notes", ...
        match = folder if folder in existing else next((e for e in existing if e.endswith(" " + suffix)), None)
        folders[folder] = match or folder
    return root, folders


def push_media(root, media_out):
    """One adb push of the whole Media tree; adb merges it into the existing folders."""
    n = sum(len(f) for _, _, f in os.walk(os.path.join(media_out, "Media")))
    print(f"pushing {n} media files to {root}/Media ...")
    adb("push", os.path.join(media_out, "Media"), f"{root}/")
    print(f"pushed: {n} files")


CHAT_MSG_COLS = ["display_message_row_id", "last_message_row_id", "last_read_message_row_id",
                 "last_read_receipt_sent_message_row_id", "last_important_message_row_id"]
CHAT_SORT_COLS = ["display_message_sort_id", "last_message_sort_id", "last_read_message_sort_id",
                  "last_read_receipt_sent_message_sort_id"]


def chat_cols(cur):
    return {r[1] for r in cur.execute("PRAGMA table_info(chat)")}


def b36(n):
    s, digits = "", "0123456789abcdefghijklmnopqrstuvwxyz"
    while True:
        n, r = divmod(n, 36)
        s = digits[r] + s
        if not n:
            return s


def drop_existing(con, chat_id, rows, window=60000, type_window=3000, long_window=None):
    """Rows not already in the chat. A db message from the same side with the same text within 60s (the two
    phones' clocks and an export made by the other side can be that far apart), or with the same media/deleted
    type within 3s, is the same message. Each db message stands for at most one export row, the closest.
    long_window, if given, is the allowance for texts of 6+ characters (a message delivered late)."""
    norm = lambda t: (t or "").replace(LRM, "").strip()
    by_min = {}  # (from_me, minute) -> [[ts, type, text, used]]
    for ts, from_me, mtype, text in con.execute("SELECT timestamp, from_me, message_type, text_data "
                                                "FROM message WHERE chat_row_id=? AND timestamp>0", (chat_id,)):
        by_min.setdefault((from_me, ts // 60000), []).append([ts, mtype, norm(text), False])
    keep = []
    for r in rows:
        text = norm(r["text"])
        win = long_window if long_window and len(text) >= 6 else window
        reach = max(win, type_window) // 60000 + 1
        near = [h for d in range(-reach, reach + 1) for h in by_min.get((r["from_me"], r["ts"] // 60000 + d), []) if not h[3]]
        same = [h for h in near if (text and h[2] == text and abs(h[0] - r["ts"]) <= win) or
                (r["type"] and h[1] == r["type"] and abs(h[0] - r["ts"]) <= type_window)]
        if same:
            min(same, key=lambda h: abs(h[0] - r["ts"]))[3] = True
        else:
            keep.append(r)
    return keep


def renumber_sort_ids(cur):
    """Number every message 0..n-1 in time order and repoint the chats' *_sort_id columns. The app gives each
    new message sort_id = its row id, so sort ids must stay at or below the highest row id - otherwise new
    messages sort above older ones."""
    order = cur.execute("SELECT _id, sort_id FROM message ORDER BY timestamp > 0, "
                        "CASE WHEN timestamp > 0 THEN timestamp ELSE sort_id END, sort_id, _id").fetchall()
    cur.executemany("UPDATE message SET sort_id=? WHERE _id=?", [(i, mid) for i, (mid, s) in enumerate(order) if s != i])
    have = chat_cols(cur)
    for rc, sc in zip(CHAT_MSG_COLS, CHAT_SORT_COLS):  # display, last, last_read, last_read_receipt_sent
        if rc in have and sc in have:
            cur.execute(f"UPDATE chat SET {sc}=(SELECT sort_id FROM message WHERE _id=chat.{rc}) "
                        f"WHERE {rc} IN (SELECT _id FROM message)")


def point_chat_at_last(cur, chat_id):
    """Make the chat show (and count as read) its newest message after an import added newer ones."""
    last_id, last_sort, last_ts = cur.execute(
        "SELECT _id, sort_id, timestamp FROM message WHERE chat_row_id=? ORDER BY sort_id DESC LIMIT 1", (chat_id,)).fetchone()
    have = chat_cols(cur)
    sets = {c: last_id for c in CHAT_MSG_COLS if c in have} | {c: last_sort for c in CHAT_SORT_COLS if c in have}
    sets["sort_timestamp"] = last_ts
    cur.execute(f'UPDATE chat SET {",".join(c + "=?" for c in sets)} WHERE _id=?', [*sets.values(), chat_id])


def new_key():
    return "3A" + secrets.token_hex(9).upper()


def insert_rows(cur, chat_id, rows, media_out, folders, tmp):
    # temporary sort ids after everything; renumber_sort_ids() puts them in time order afterwards
    first = cur.execute("SELECT coalesce(max(sort_id), 0) + 1 FROM message").fetchone()[0]
    chat_jid = cur.execute("SELECT jid_row_id FROM chat WHERE _id=?", (chat_id,)).fetchone()[0]
    ids = []
    for i, r in enumerate(rows):
        sort_id = first + i
        rid = cur.execute(
            """INSERT INTO message(chat_row_id, from_me, key_id, sender_jid_row_id, status, broadcast, recipient_count,
                   origination_flags, origin, timestamp, received_timestamp, receipt_server_timestamp, message_type,
                   text_data, starred, lookup_tables, message_add_on_flags, view_mode, sort_id)
               VALUES(?,?,?,0,?,0,0,65536,0,?,0,-1,?,?,0,0,0,0,?)""",
            (chat_id, r["from_me"], new_key(), 5 if r["from_me"] else 0, r["ts"], r["type"], r["text"], sort_id)).lastrowid
        ids.append(rid)
        if r["type"] == 15:
            cur.execute("INSERT INTO message_revoked(message_row_id, revoked_key_id, revoke_timestamp) VALUES(?,?,?)",
                        (rid, new_key(), r["ts"]))
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
            write_media_details(cur, rid, media_details(r["type"], md["path"], tmp))
            if media_out:
                dst = os.path.join(media_out, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(md["path"], dst)
        if r["text"]:
            # docid is the message _id; fts_jid is "<sender> <chat>" (1 = me, 0 = the other side of a 1:1 chat)
            cur.execute("INSERT INTO message_ftsv2(docid, content, fts_jid, fts_namespace) VALUES(?,?,?,'')",
                        (rid, r["text"], f"{r['from_me']} {b36(chat_jid + 10)}"))
    renumber_sort_ids(cur)
    return ids


def backup(db):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = f"{db}.bak-{stamp}"
    os.makedirs(dst)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            shutil.copy2(db + suffix, os.path.join(dst, os.path.basename(db) + suffix))
    return dst


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="path to msgstore.db")
    p.add_argument("--export", required=True, help="exported chat: .zip, folder, or _chat.txt")
    p.add_argument("--contact", required=True, help="phone number with country code, or contact name (looked up in wa.db)")
    p.add_argument("--me", help="your name as it appears in the export (auto-detected when possible; pass it for an "
                                "export made by the other person, where the \"You ...\" lines are theirs)")
    p.add_argument("--wa-db", help="path to wa.db for name lookup (default: next to msgstore.db)")
    p.add_argument("--tz", help="timezone the export's times are in, e.g. Europe/Berlin (default: this computer's)")
    p.add_argument("--date-order", choices=["auto", "dmy", "mdy"], default="auto")
    p.add_argument("--media-out", help="folder to copy media into, laid out as Media/... for copying to the phone "
                                       "(default: <export name>-media next to the export)")
    p.add_argument("--push", action="store_true", help="with --apply: copy the media to the phone over adb, "
                                                          "matching the app's own media folder names")
    p.add_argument("--package", default="com.whatsapp", help="app package, for --push (default: com.whatsapp)")
    p.add_argument("--phone-root", help="app media root on the phone, for --push "
                                        "(default: auto-detect under /sdcard/Android/media/<package>/)")
    p.add_argument("--apply", action="store_true", help="actually write (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)

    if not os.path.exists(a.db):
        die(f"{a.db} not found")
    tz = ZoneInfo(a.tz) if a.tz else None  # None: local time
    wa_db = a.wa_db or os.path.join(os.path.dirname(os.path.abspath(a.db)), "wa.db")

    with tempfile.TemporaryDirectory() as tmp:
        txt, media_dir = open_export(a.export, tmp)
        msgs, fmt = parse_lines(txt)
        if not msgs:
            die("no messages found in export - unsupported format?")
        senders = sorted({m["sender"] for m in msgs})
        me = a.me
        if not me:
            mine = {m["sender"] for m in msgs if re.match(r"^‎*You (deleted|turned|changed|blocked)", m["body"])}
            if len(mine) == 1:
                me = mine.pop()
            else:
                die(f"can't tell which sender is you, pass --me (senders: {senders})")
        if me not in senders:
            die(f"--me '{me}' is not a sender in the export (senders: {senders})")
        if len(senders) > 2:
            die(f"export has {len(senders)} senders - group chats are not supported")
        order = detect_date_order(msgs, a.date_order)
        rows, skipped = classify(msgs, fmt, me, order, tz, media_dir)
        rows.sort(key=lambda r: r["ts"])  # stable: keeps export order within a second

        con = sqlite3.connect(a.db)
        chat_id, number = find_chat(con, a.contact, wa_db)
        span = chat_id and con.execute("SELECT min(timestamp), max(timestamp), count(*) FROM message "
                                       "WHERE chat_row_id=? AND timestamp>0", (chat_id,)).fetchone()
        already = 0
        if chat_id:
            # exports without seconds are up to a minute behind the real time
            minutes_only = not any(m["time"].count(":") + m["time"].count(".") >= 2 for m in msgs[:200])
            keep = drop_existing(con, chat_id, rows, *((120000, 61000) if minutes_only else (60000, 3000)))
            already, rows = len(rows) - len(keep), keep

        fmt_ts = lambda ms: datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d %H:%M")
        print(f"export: {fmt} format, {len(msgs)} entries, you = '{me}', dates {order}, tz {a.tz or 'local'}")
        print(f"chat:   {'existing chat ' + str(chat_id) if chat_id else 'NEW chat for ' + number}"
              + (f", {span[2]} messages in db {fmt_ts(span[0])} -> {fmt_ts(span[1])}" if span and span[2] else ""))
        print(f"skip:   {already} already covered by db, " + ", ".join(f"{v} {k}" for k, v in skipped.items() if v))
        kinds = {0: "text", 1: "image", 2: "audio", 3: "video", 4: "contact", 9: "document", 13: "gif", 15: "deleted",
                 20: "sticker"}
        counts = {}
        for r in rows:
            counts[kinds[r["type"]]] = counts.get(kinds[r["type"]], 0) + 1
        if rows:
            print(f"import: {len(rows)} messages {fmt_ts(rows[0]['ts'])} -> {fmt_ts(rows[-1]['ts'])} "
                  f"({sum(r['from_me'] for r in rows)} from you) " + ", ".join(f"{v} {k}" for k, v in counts.items()))

        export_abs = os.path.abspath(a.export.rstrip("/"))
        media_out = a.media_out or os.path.join(os.path.dirname(export_abs),
                                                re.sub(r"\.(zip|txt)$", "", os.path.basename(export_abs)) + "-media")
        root, folders = (None, {})
        if a.push and a.apply:
            root, folders = phone_media_layout(a.package, a.phone_root)
            print(f"phone:  media root {root}")

        # Everything happens in one transaction; a dry run rolls it back (and copies no files).
        bak = backup(a.db) if a.apply else None
        cur = con.cursor()
        cur.execute("BEGIN")
        try:
            touched = filled = 0
            if chat_id is not None:
                touched, filled = sync_existing_media(cur, chat_id, media_dir, tmp, media_out if a.apply else None, folders)
            new_chat = chat_id is None and bool(rows)
            if new_chat:
                chat_id = create_chat(cur, number, rows[0]["ts"], rows[-1]["ts"])
            if rows:
                insert_rows(cur, chat_id, rows, media_out if a.apply else None, folders, tmp)
                if new_chat:
                    point_chat_at_last(cur, chat_id)
                else:
                    cur.execute("UPDATE chat SET created_timestamp=min(coalesce(nullif(created_timestamp, 0), ?), ?) "
                                "WHERE _id=?", (rows[0]["ts"], rows[0]["ts"], chat_id))
                    point_chat_at_last(cur, chat_id)
            if filled:
                print(f"media:  {filled} already-imported media messages get previews/durations filled in")
            if not rows and not filled and not (a.push and touched):
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
        if rows:
            print(f"done:   imported {len(rows)} messages into chat {chat_id}")
        has_media = touched or any(r["media"] for r in rows)
        if has_media and root:
            push_media(root, media_out)
        elif has_media:
            print(f"media:  copied to {media_out}/Media - copy its contents into the app's media folder on the phone\n"
                  f"        (Android/media/{a.package}/<app folder>/Media), or re-run with --apply --push with the phone connected")
        if new_chat:
            print("note:   created a new chat keyed by phone number; WhatsApp may later show a separate chat if it uses an @lid id")


if __name__ == "__main__":
    main()
