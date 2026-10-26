"""Work with unencrypted iPhone backups (the Finder / iTunes / 3uTools / dr.fone folder holding Manifest.db).

  info      what the backup is: device, iOS, date, encryption, WhatsApp data
  extract   copy WhatsApp's files out of the backup into a plain folder (ChatStorage.sqlite, Message/, Sticker.sqlite)
  check     find records that would make a restore fail; --fix corrects them
  backup    make a new full unencrypted backup of the connected iPhone (idevicebackup2)
  restore   restore a full backup to the connected iPhone (idevicebackup2), after checking it

A restore always replaces the whole iPhone with what the backup holds. Never restore a backup that was cut down
to one app: the phone comes back with only that app's data. Restore the full backup, edited in place.

Restores fail with MBErrorDomain 205 when a record's size or digest does not match its file, or a file is missing
- common in backups made by third-party tools while apps were writing. `check --fix` repairs that.

examples:
  wakit ios-backup info --backup ~/Backups/<device-id>
  wakit ios-backup extract --backup ~/Backups/<device-id> --out iphone-whatsapp/
  wakit ios-backup check --backup ~/Backups/<device-id> --fix
  wakit ios-backup backup --out ~/Backups
  wakit ios-backup restore --backup ~/Backups/<device-id> --yes
"""
import argparse, hashlib, os, plistlib, shutil, sqlite3, subprocess, sys

WA_SHARED = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"


def die(msg):
    sys.exit(f"error: {msg}")


def open_backup(root):
    if not os.path.exists(os.path.join(root, "Manifest.db")):
        die(f"no Manifest.db in {root} - point at the backup folder itself (named after the device id)")
    return sqlite3.connect(os.path.join(root, "Manifest.db"))


XML_HEADER = b'<?xml version="1.0" encoding="UTF-8"?>\n'


def read_plist(path):
    """plistlib.load that also takes the header-less XML some backup tools write"""
    data = open(path, "rb").read()
    try:
        return plistlib.loads(data)
    except plistlib.InvalidFileException:
        return plistlib.loads(data, fmt=plistlib.FMT_XML)


def manifest_info(root):
    m = read_plist(os.path.join(root, "Manifest.plist"))
    info = {}
    if os.path.exists(os.path.join(root, "Info.plist")):
        info = read_plist(os.path.join(root, "Info.plist"))
    lock = m.get("Lockdown", {})
    return {"encrypted": bool(m.get("IsEncrypted")), "device": info.get("Device Name") or lock.get("DeviceName"),
            "product": info.get("Product Type") or lock.get("ProductType"),
            "ios": info.get("Product Version") or lock.get("ProductVersion"),
            "date": info.get("Last Backup Date") or m.get("Date"),
            "udid": info.get("Unique Identifier") or lock.get("UniqueDeviceID") or os.path.basename(root.rstrip("/"))}


def file_path(root, fid):
    return os.path.join(root, fid[:2], fid)


def attrs_of(blob):
    """(archive, attribute dict, its objects) of an MBFile record"""
    arch = plistlib.loads(blob)
    objs = arch["$objects"]
    return arch, next(o for o in objs if isinstance(o, dict) and "Mode" in o), objs


def extract(root, out, domains=(WA_SHARED,)):
    """copy the files of the given domains out of the backup, under their real paths; returns the count"""
    man = open_backup(root)
    if manifest_info(root)["encrypted"]:
        die("the backup is encrypted - make an unencrypted one")
    n = 0
    q = f"SELECT fileID, domain, relativePath FROM Files WHERE flags=1 AND domain IN ({','.join('?' * len(domains))})"
    for fid, domain, rel in man.execute(q, domains):
        src = file_path(root, fid)
        if not os.path.exists(src):
            continue
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        n += 1
    man.close()
    return n


def check(root, fix=False):
    """problems that make a restore fail: missing files, wrong sizes, wrong digests; fixes them with fix=True"""
    man = open_backup(root)
    missing, wrong, updates = [], [], []
    for fid, domain, rel, blob in man.execute("SELECT fileID, domain, relativePath, file FROM Files WHERE flags=1"):
        path = file_path(root, fid)
        if not os.path.exists(path):
            missing.append((fid, domain, rel))
            continue
        arch, attrs, objs = attrs_of(blob)
        size, changed = os.path.getsize(path), False
        if attrs.get("Size") != size:
            attrs["Size"], changed = size, True
        if "Digest" in attrs:
            d = attrs["Digest"]
            want = objs[d.data] if isinstance(d, plistlib.UID) else d
            got = hashlib.sha1(open(path, "rb").read()).digest()
            if got != want:
                if isinstance(d, plistlib.UID):
                    objs[d.data] = got
                else:
                    attrs["Digest"] = got
                changed = True
        if changed:
            wrong.append((domain, rel))
            updates.append((plistlib.dumps(arch, fmt=plistlib.FMT_BINARY), fid))
    info = os.path.join(root, "Info.plist")
    bad_info = os.path.exists(info) and open(info, "rb").read(6) not in (b"<?xml ", b"bplist")
    if bad_info:
        wrong.append(("Info.plist", "has no <?xml?> header, so idevicebackup2 cannot read it"))
        if fix:
            data = open(info, "rb").read()
            open(info, "wb").write(XML_HEADER + data)
    if fix and (missing or updates):
        man.executemany("DELETE FROM Files WHERE fileID=?", [(f,) for f, _, _ in missing])
        man.executemany("UPDATE Files SET file=? WHERE fileID=?", updates)
        man.commit()
    man.close()
    return missing, wrong


def idevicebackup2():
    tool = shutil.which("idevicebackup2")
    if not tool:
        die("needs idevicebackup2 from libimobiledevice (macOS: brew install libimobiledevice; "
            "Linux: apt install libimobiledevice-utils)")
    return tool


def connected_udid():
    r = subprocess.run(["idevice_id", "-l"], capture_output=True, text=True)
    ids = r.stdout.split()
    if not ids:
        die("no iPhone connected - plug it in, unlock it and tap Trust")
    if len(ids) > 1:
        die(f"more than one iPhone connected: {ids}")
    return ids[0]


def cmd_info(a):
    i = manifest_info(a.backup)
    man = open_backup(a.backup)
    total = man.execute("SELECT count(*) FROM Files").fetchone()[0]
    wa = man.execute("SELECT count(*) FROM Files WHERE domain LIKE '%whatsapp%'").fetchone()[0]
    chat = man.execute("SELECT fileID FROM Files WHERE domain=? AND relativePath='ChatStorage.sqlite'",
                       (WA_SHARED,)).fetchone()
    print(f"device:    {i['device']} ({i['product']}, iOS {i['ios']}), id {i['udid']}")
    print(f"date:      {i['date']}")
    print(f"encrypted: {'yes - wakit cannot read it' if i['encrypted'] else 'no'}")
    print(f"files:     {total} ({wa} WhatsApp)")
    if chat and os.path.exists(file_path(a.backup, chat[0])):
        con = sqlite3.connect(f"file:{file_path(a.backup, chat[0])}?immutable=1", uri=True)
        n, s = con.execute("SELECT (SELECT count(*) FROM ZWAMESSAGE), (SELECT count(*) FROM ZWACHATSESSION)").fetchone()
        print(f"whatsapp:  {n} messages in {s} chats")
    else:
        print("whatsapp:  not in this backup")


def cmd_extract(a):
    n = extract(a.backup, a.out)
    print(f"extracted {n} WhatsApp files to {a.out}")
    if n:
        print(f"next: wakit from-ios --db msgstore.db --backup {a.out}")


def cmd_check(a):
    missing, wrong = check(a.backup, a.fix)
    for _, d, r in missing[:10]:
        print(f"missing:   {d} {r}")
    for d, r in wrong[:10]:
        print(f"mismatch:  {d} {r}")
    more = len(missing) + len(wrong) - min(len(missing), 10) - min(len(wrong), 10)
    if more > 0:
        print(f"           ... and {more} more")
    print(f"{len(missing)} missing files, {len(wrong)} records with the wrong size or digest")
    if missing or wrong:
        print("fixed - the backup will restore cleanly" if a.fix else "re-run with --fix to correct them")
    else:
        print("ok - nothing would stop a restore")


def cmd_backup(a):
    tool, udid = idevicebackup2(), connected_udid()
    os.makedirs(a.out, exist_ok=True)
    enc = subprocess.run(["ideviceinfo", "-u", udid, "-q", "com.apple.mobile.backup", "-k", "WillEncrypt"],
                         capture_output=True, text=True).stdout.strip()
    if enc == "true":
        die("backup encryption is on for this iPhone - turn it off first (idevicebackup2 encryption off <password>)")
    print(f"backing up {udid} into {a.out} - keep the iPhone unlocked and connected")
    sys.exit(subprocess.call([tool, "-u", udid, "backup", "--full", a.out]))


def cmd_restore(a):
    tool = idevicebackup2()
    root = os.path.abspath(a.backup.rstrip("/"))
    udid = os.path.basename(root)
    info = manifest_info(root)
    if info["encrypted"]:
        die("encrypted backups are not supported here")
    man = open_backup(root)
    domains = {d for (d,) in man.execute("SELECT DISTINCT domain FROM Files")}
    if not {"HomeDomain", "CameraRollDomain"} & domains:
        die("this backup has no HomeDomain/CameraRollDomain - it looks cut down to some apps. Restoring it would "
            "leave the iPhone with only those apps' data. Restore a full backup instead.")
    missing, wrong = check(root)
    if missing or wrong:
        die(f"{len(missing)} missing files and {len(wrong)} bad records would make the restore fail - "
            f"run: wakit ios-backup check --backup '{a.backup}' --fix")
    target = connected_udid()
    print(f"backup:  {info['device']}, iOS {info['ios']}, from {info['date']}")
    print(f"iPhone:  {target}")
    print("This REPLACES EVERYTHING on the iPhone with the backup's contents. Find My must be off, and the iPhone "
          "must stay unlocked (Auto-Lock: Never) until it reboots.")
    if not a.yes:
        die("pass --yes to restore")
    cmd = [tool, "-u", target, "-s", udid, "restore", "--system", "--settings", os.path.dirname(root)]
    sys.exit(subprocess.call(cmd))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("info", help="describe a backup")
    s.add_argument("--backup", required=True, help="backup folder (holds Manifest.db)")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("extract", help="copy WhatsApp's files out of a backup")
    s.add_argument("--backup", required=True, help="backup folder (holds Manifest.db)")
    s.add_argument("--out", required=True, help="folder to write ChatStorage.sqlite, Message/ ... into")
    s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("check", help="find (and --fix) records that would make a restore fail")
    s.add_argument("--backup", required=True, help="backup folder (holds Manifest.db)")
    s.add_argument("--fix", action="store_true", help="correct the records in place")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("backup", help="make a full unencrypted backup of the connected iPhone")
    s.add_argument("--out", required=True, help="folder to put the backup in (a sub-folder named by device id)")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="restore a full backup to the connected iPhone")
    s.add_argument("--backup", required=True, help="backup folder (named after the device id)")
    s.add_argument("--yes", action="store_true", help="confirm: everything on the iPhone is replaced")
    s.set_defaults(fn=cmd_restore)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()


def resolve(path):
    """a folder with ChatStorage.sqlite as is; a full backup (Manifest.db) is extracted to a temp folder first"""
    if not os.path.exists(os.path.join(path, "Manifest.db")):
        return path
    import atexit, tempfile
    tmp = tempfile.mkdtemp(prefix="wakit-ios-")
    atexit.register(shutil.rmtree, tmp, True)
    print(f"backup: extracting WhatsApp's files from the full backup ...")
    extract(path, tmp)
    return tmp
