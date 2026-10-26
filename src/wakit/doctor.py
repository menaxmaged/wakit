"""Check a WhatsApp file before you change it: what it is, what's in it, and whether wakit knows its layout.

Point --path at any of:
  msgstore.db / wa.db / stickers.db        Android databases
  ChatStorage.sqlite                       the iPhone database
  a folder with Manifest.db                an unencrypted iPhone backup (also checked for restore problems)
  msgstore.db.crypt14 / .crypt15           an Android encrypted backup (add --key to look inside)
  an "Export chat" .zip / .txt / folder

WhatsApp changes its database layout every few months. If a table or column wakit writes to is missing, doctor
says so and exits with status 1, and every wakit command that writes runs the same check first and refuses to go
on. To report a new layout, open an issue with doctor's output; it shows table and column names, never messages.

examples:
  wakit doctor --path msgstore.db
  wakit doctor --path ~/Backups/<device-id>
  wakit doctor --path msgstore.db.crypt15 --key "<64-digit key>"
"""
import argparse, os, sqlite3, sys, tempfile

# what wakit reads and writes, per kind of database: {table: [columns]}
LAYOUTS = {
    "android": {
        "message": ["_id", "chat_row_id", "from_me", "key_id", "sender_jid_row_id", "status", "broadcast",
                    "recipient_count", "origination_flags", "origin", "timestamp", "received_timestamp",
                    "receipt_server_timestamp", "message_type", "text_data", "starred", "lookup_tables",
                    "message_add_on_flags", "view_mode", "sort_id"],
        "chat": ["_id", "jid_row_id", "account_jid_row_id", "hidden", "archived", "subject", "created_timestamp",
                 "sort_timestamp", "display_message_row_id", "display_message_sort_id", "last_message_row_id",
                 "last_message_sort_id", "last_read_message_row_id", "last_read_message_sort_id",
                 "last_read_receipt_sent_message_row_id", "last_read_receipt_sent_message_sort_id"],
        "jid": ["_id", "user", "server", "agent", "device", "type", "raw_string"],
        "jid_map": ["lid_row_id", "jid_row_id"],
        "message_media": ["message_row_id", "chat_row_id", "autotransfer_retry_enabled", "transferred", "file_path",
                          "file_size", "mime_type", "file_length", "media_name", "file_hash", "width", "height",
                          "media_duration"],
        "message_ftsv2": ["content", "fts_jid", "fts_namespace"],
        "message_quoted": ["message_row_id", "key_id", "from_me", "sender_jid_row_id", "text_data"],
        "message_vcard": ["message_row_id", "vcard"],
        "message_revoked": ["message_row_id", "revoked_key_id", "revoke_timestamp"],
        "message_location": ["message_row_id", "latitude", "longitude", "place_name"],
    },
    "ios": {
        "ZWACHATSESSION": ["Z_PK", "Z_ENT", "Z_OPT", "ZARCHIVED", "ZCONTACTABID", "ZFLAGS", "ZHIDDEN",
                           "ZIDENTITYVERIFICATIONEPOCH", "ZIDENTITYVERIFICATIONSTATE", "ZMESSAGECOUNTER", "ZREMOVED",
                           "ZSESSIONTYPE", "ZSPOTLIGHTSTATUS", "ZUNREADCOUNT", "ZGROUPINFO", "ZLASTMESSAGE",
                           "ZLASTMESSAGEDATE", "ZLASTMESSAGETEXT", "ZCONTACTIDENTIFIER", "ZCONTACTJID",
                           "ZPARTNERNAME", "ZETAG"],
        "ZWAMESSAGE": ["Z_PK", "Z_ENT", "Z_OPT", "ZCHILDMESSAGESDELIVEREDCOUNT", "ZCHILDMESSAGESPLAYEDCOUNT",
                       "ZCHILDMESSAGESREADCOUNT", "ZDATAITEMVERSION", "ZDOCID", "ZENCRETRYCOUNT",
                       "ZFILTEREDRECIPIENTCOUNT", "ZFLAGS", "ZGROUPEVENTTYPE", "ZISFROMME", "ZMESSAGEERRORSTATUS",
                       "ZMESSAGESTATUS", "ZMESSAGETYPE", "ZSORT", "ZSPOTLIGHTSTATUS", "ZSTARRED", "ZCHATSESSION",
                       "ZGROUPMEMBER", "ZLASTSESSION", "ZMEDIAITEM", "ZMESSAGEDATE", "ZSENTDATE", "ZFROMJID",
                       "ZMEDIASECTIONID", "ZPUSHNAME", "ZSTANZAID", "ZTEXT", "ZTOJID"],
        "ZWAMEDIAITEM": ["Z_PK", "Z_ENT", "Z_OPT", "ZCLOUDSTATUS", "ZFILESIZE", "ZMEDIAORIGIN", "ZMOVIEDURATION",
                         "ZMESSAGE", "ZASPECTRATIO", "ZHACCURACY", "ZLATITUDE", "ZLONGITUDE", "ZAUTHORNAME", "ZTITLE",
                         "ZMEDIALOCALPATH", "ZXMPPTHUMBPATH", "ZVCARDNAME", "ZVCARDSTRING", "ZMETADATA"],
        "ZWAGROUPMEMBER": ["Z_PK", "Z_ENT", "Z_OPT", "ZCONTACTABID", "ZISACTIVE", "ZISADMIN", "ZCHATSESSION",
                           "ZMEMBERJID"],
        "ZWAGROUPINFO": ["Z_PK", "Z_ENT", "Z_OPT", "ZSTATE", "ZCHATSESSION", "ZCREATIONDATE"],
        "ZWAMESSAGE_fts": ["_id", "content"],
        "Z_PRIMARYKEY": ["Z_NAME", "Z_MAX"],
    },
    "wa": {"wa_contacts": ["jid", "display_name", "wa_name"]},
    "stickers": {"starred_stickers": ["plaintext_hash"], "recent_stickers": ["plaintext_hash"]},
}
# tables that may be missing altogether: .crypt14/.crypt15 backups leave out the search index
OPTIONAL = {"message_ftsv2"}
NAMES = {"android": "Android WhatsApp database (msgstore.db)", "ios": "iPhone WhatsApp database (ChatStorage.sqlite)",
         "wa": "Android contacts database (wa.db)", "stickers": "Android stickers database (stickers.db)"}


def die(msg):
    sys.exit(f"error: {msg}")


def columns(con):
    """{table: {columns}} of an open database (FTS virtual tables included)"""
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            out[t] = {r[1] for r in con.execute(f'PRAGMA table_info("{t}")')}
        except sqlite3.Error:  # a virtual table whose module this Python lacks
            out[t] = set()
    return out


def detect(con):
    have = columns(con)
    for kind, marker in (("ios", "ZWAMESSAGE"), ("android", "message"), ("wa", "wa_contacts"),
                         ("stickers", "starred_stickers")):
        if marker in have and (kind != "android" or "jid" in have):
            return kind, have
    return None, have


def missing(have, kind):
    """[(table, [missing columns])] - a missing table is listed with all its columns"""
    out = []
    for table, cols in LAYOUTS[kind].items():
        if table not in have:
            if table not in OPTIONAL:
                out.append((table, cols))
        else:
            gone = [c for c in cols if c not in have[table]]
            if gone:
                out.append((table, gone))
    return out


def require(con, kind, label="the database"):
    """refuse to write into a database whose layout wakit does not know; called by every command that writes"""
    found, have = detect(con)
    if found != kind:
        die(f"{label} is not a {NAMES[kind]}" + (f" - it looks like a {NAMES[found]}" if found else ""))
    gaps = missing(have, kind)
    if gaps:
        lines = "\n".join(f"  {t}: {', '.join(c)}" for t, c in gaps)
        die(f"{label} has a WhatsApp layout wakit doesn't know yet - nothing was changed. Missing:\n{lines}\n"
            f"Please open an issue with this list (and `wakit doctor` output): "
            f"https://github.com/menaxmaged/wakit/issues")


def report_sqlite(path, label=None):
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        print(f"file:      {label or path}\nerror:     not a readable SQLite database ({e})")
        return False
    kind, have = detect(con)
    print(f"file:      {label or path}")
    print(f"is:        {NAMES.get(kind, 'an SQLite database, but not one wakit knows')}")
    print(f"integrity: {ok}")
    if kind == "android":
        n, c, j = con.execute("SELECT (SELECT count(*) FROM message), (SELECT count(*) FROM chat), "
                              "(SELECT count(*) FROM jid WHERE server='lid')").fetchone()
        mx, ms = con.execute("SELECT max(_id), max(sort_id) FROM message").fetchone()
        print(f"contents:  {n} messages in {c} chats; {'uses @lid ids' if j else 'phone-number ids only'}")
        if mx is not None and ms is not None and ms > mx:
            print("warning:   sort_id above the largest _id - new messages would sort into the middle of chats "
                  "(any wakit import fixes this)")
    elif kind == "ios":
        n, c = con.execute("SELECT (SELECT count(*) FROM ZWAMESSAGE), (SELECT count(*) FROM ZWACHATSESSION)").fetchone()
        print(f"contents:  {n} messages in {c} chats")
    elif kind == "wa":
        print(f"contents:  {con.execute('SELECT count(*) FROM wa_contacts').fetchone()[0]} contacts")
    elif kind == "stickers":
        s, r = con.execute("SELECT (SELECT count(*) FROM starred_stickers), "
                           "(SELECT count(*) FROM recent_stickers)").fetchone()
        print(f"contents:  {s} favourite, {r} recent stickers")
    healthy = ok == "ok"
    if kind:
        gaps = missing(have, kind)
        if gaps:
            healthy = False
            print("layout:    NOT RECOGNISED - wakit will refuse to write into it. Missing:")
            for t, cols in gaps:
                print(f"             {t}: {', '.join(cols)}")
        else:
            print("layout:    recognised - wakit can work with it")
    con.close()
    return healthy and kind is not None


def report_backup(path):
    from . import ios_backup
    info = ios_backup.manifest_info(path)
    print(f"backup:    {info['device']} ({info['product']}, iOS {info['ios']}), from {info['date']}")
    if info["encrypted"]:
        print("encrypted: yes - wakit can't read encrypted backups; make an unencrypted one")
        return False
    man = ios_backup.open_backup(path)
    domains = {d for (d,) in man.execute("SELECT DISTINCT domain FROM Files")}
    chat = man.execute("SELECT fileID FROM Files WHERE domain=? AND relativePath='ChatStorage.sqlite'",
                       (ios_backup.WA_SHARED,)).fetchone()
    man.close()
    full = bool({"HomeDomain", "CameraRollDomain"} & domains)
    print(f"kind:      {'full backup' if full else 'PARTIAL (only some apps) - never restore this one'}")
    miss, wrong = ios_backup.check(path)
    print(f"restore:   " + ("ok - nothing would stop a restore" if not (miss or wrong) else
                            f"{len(miss)} missing files, {len(wrong)} bad records - run `wakit ios-backup check --fix`"))
    if not chat or not os.path.exists(ios_backup.file_path(path, chat[0])):
        print("whatsapp:  not in this backup")
        return False
    print()
    return report_sqlite(ios_backup.file_path(path, chat[0]), "WhatsApp's ChatStorage.sqlite in the backup") and full


def report_crypt(path, key):
    from . import crypt
    data = open(path, "rb").read()
    head = crypt.parse(data)
    print(f"file:      {path}\nis:        Android encrypted backup ({head['kind']}"
          + (f", WhatsApp {head['version']})" if head["version"] else ")"))
    if not key:
        print("contents:  pass --key to look inside (the 64-digit key, or the key file)")
        return True
    db, _ = crypt.decrypt(data, crypt.load_key(key, head["kind"]), head)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "inside.db")
        open(p, "wb").write(db)
        print()
        return report_sqlite(p, "the database inside")


def report_export(path):
    from . import export_import as w
    with tempfile.TemporaryDirectory() as tmp:
        txt, media = w.open_export(path, tmp)
        msgs, fmt = w.parse_lines(txt)
        senders = sorted({m["sender"] for m in msgs})
        order = w.detect_date_order(msgs, "auto") if msgs else "?"
        files = len([f for f in os.listdir(media) if not f.endswith(".txt")]) if media and os.path.isdir(media) else 0
    print(f"file:      {path}\nis:        WhatsApp chat export ({fmt or 'unknown'} format, dates {order})")
    print(f"contents:  {len(msgs)} lines from {len(senders)} senders: {', '.join(senders[:6])}"
          + (" ..." if len(senders) > 6 else "") + f"; {files} media files")
    if len(senders) > 2:
        print("note:      more than two senders - group exports are not supported by `wakit import`")
    return bool(msgs)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", required=True, help="database, iPhone backup folder, .crypt14/.crypt15, or chat export")
    p.add_argument("--key", help="for .crypt14/.crypt15: the 64-digit key or key file, to check what's inside")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    path = os.path.expanduser(a.path)
    if not os.path.exists(path):
        die(f"{a.path} not found")
    if os.path.isdir(path) and os.path.exists(os.path.join(path, "Manifest.db")):
        ok = report_backup(path)
    elif os.path.isdir(path) or path.lower().endswith((".zip", ".txt")):
        ok = report_export(path)
    elif open(path, "rb").read(16) == b"SQLite format 3\x00":
        ok = report_sqlite(path)
    elif ".crypt1" in os.path.basename(path):
        ok = report_crypt(path, a.key)
    else:
        die(f"{a.path}: not a WhatsApp database, backup or export wakit knows")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
