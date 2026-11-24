"""Back up and restore WhatsApp's own app data over adb, with no root (experimental).

  info       is the phone connected, which Android and WhatsApp does it run, and can `adb backup` include WhatsApp
  downgrade  save the installed WhatsApp here, then swap in an old WhatsApp that allows adb backup (chats are kept)
  backup     adb backup WhatsApp's data into a .ab file (you confirm on the phone)
  extract    .ab file -> msgstore.db, wa.db, stickers.db and the `key` file (which also opens .crypt14 backups)
  pack       the original .ab + your edited msgstore.db -> a new .ab to restore
  restore    adb restore a .ab file (you confirm on the phone): replaces WhatsApp's data
  upgrade    reinstall the WhatsApp saved by downgrade (or update it from the Play Store)

Why an old WhatsApp: current WhatsApp opts out of `adb backup` (allowBackup=false), and Android 12+ leaves out the
data of every app built for Android 12+. An old official WhatsApp (for example 2.11.431) has neither problem. With
the old version installed over the kept data, `adb backup` copies the whole data folder, and `adb restore` puts an
edited copy back. The old app is never opened: Android runs backup and restore without starting the app's own code.
Then the current WhatsApp goes back on top and opens the restored chats.

Before you start:
  - Make a normal WhatsApp backup first (Settings > Chats > Chat backup). If anything goes wrong, reinstall
    WhatsApp and restore from it.
  - Put the phone in airplane mode until `upgrade` is done, and don't open WhatsApp in between.
  - You need an old WhatsApp APK with allowBackup. wakit doesn't ship one: WhatsApp's APKs can't be
    redistributed. `info` shows whether the installed app already allows backups.

Android 11 and newer often refuse to install an older version over kept data (INSTALL_FAILED_VERSION_DOWNGRADE).
`downgrade` then puts your current WhatsApp straight back, and the no-root route that's left is
`wakit crypt` with a .crypt15 backup. Android 14 and newer also refuse old apps unless the install passes
--bypass-low-target-sdk-block, which wakit adds for you.

.ab format: "ANDROID BACKUP" / version / compressed / "none" or "AES-256" header lines, then (encrypted:
PBKDF2-SHA1 + AES-256-CBC) zlib-compressed tar of apps/<package>/{_manifest, db/, f/, sp/, r/}.

examples:
  wakit adb-backup info
  wakit adb-backup downgrade --apk WhatsApp-2.11.431.apk --save-to wa-apks/ --yes
  wakit adb-backup backup --out whatsapp.ab
  wakit adb-backup extract --in whatsapp.ab --out phone/
  wakit import --db phone/msgstore.db --export "WhatsApp Chat - Alex.zip" --contact 15551234567 --apply
  wakit adb-backup pack --in whatsapp.ab --db phone/msgstore.db --out whatsapp-new.ab
  wakit adb-backup restore --in whatsapp-new.ab --yes
  wakit adb-backup upgrade --apks wa-apks/
"""
import argparse, glob, hashlib, io, os, re, secrets, shutil, sqlite3, subprocess, sys, tarfile, tempfile, zlib

from .device import adb, die

MAGIC = b"ANDROID BACKUP\n"
DBS = ["msgstore.db", "wa.db", "stickers.db"]
KEYS = ["key", "encrypted_backup.key"]  # f/key opens .crypt14 backups; encrypted_backup.key opens .crypt15


# ---- the .ab file ---------------------------------------------------------------------------------------------

def _cipher():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
    except ImportError:
        die('this backup is password-protected, which needs the cryptography package: pip install "wakit[crypt]"')
    return Cipher, algorithms, modes, padding


def _aes_cbc(key, iv, data, decrypt):
    Cipher, algorithms, modes, padding = _cipher()
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    if decrypt:
        d = c.decryptor()
        raw = d.update(data) + d.finalize()
        u = padding.PKCS7(128).unpadder()
        return u.update(raw) + u.finalize()
    p = padding.PKCS7(128).padder()
    e = c.encryptor()
    return e.update(p.update(data) + p.finalize()) + e.finalize()


def _pw_bytes(chars, version):
    """how Android's PBKDF2 turns a Java char[] into bytes: UTF-8, or the low 8 bits in version 1 backups"""
    return bytes(ord(c) & 0xFF for c in chars) if version == 1 else chars.encode("utf-8", "surrogatepass")


def _checksum(master_key, salt, rounds, version):
    # Android passes the master key through a char[] of *signed* bytes: 0x80..0xFF become U+FF80..U+FFFF
    chars = "".join(chr(b if b < 0x80 else 0xFF00 | b) for b in master_key)
    return hashlib.pbkdf2_hmac("sha1", _pw_bytes(chars, version), salt, rounds, 32)


def read_ab(data, password=None):
    """.ab bytes -> (header dict, tar bytes)"""
    if not data.startswith(MAGIC):
        die("not an Android backup (.ab) file")
    lines, i = [], 0
    for _ in range(9):
        j = data.find(b"\n", i)
        if j < 0:
            die("the backup's header is cut short")
        lines.append(data[i:j].decode("ascii", "replace"))
        i = j + 1
        if len(lines) == 4 and lines[3] == "none":
            break
    version, compressed, enc = int(lines[1]), lines[2] == "1", lines[3]
    head = {"version": version, "compressed": compressed, "encrypted": enc != "none"}
    body = data[i:]
    if enc == "AES-256":
        if not password:
            die("this backup has a password: pass --password (the one you typed on the phone)")
        user_salt, ck_salt, rounds = bytes.fromhex(lines[4]), bytes.fromhex(lines[5]), int(lines[6])
        user_iv, blob = bytes.fromhex(lines[7]), bytes.fromhex(lines[8])
        user_key = hashlib.pbkdf2_hmac("sha1", _pw_bytes(password, version), user_salt, rounds, 32)
        try:
            mk = _aes_cbc(user_key, user_iv, blob, True)
        except ValueError:
            die("wrong backup password")
        n = mk[0]; iv = mk[1:1 + n]; mk = mk[1 + n:]
        n = mk[0]; master = mk[1:1 + n]; mk = mk[1 + n:]
        n = mk[0]; check = mk[1:1 + n]
        if _checksum(master, ck_salt, rounds, version) != check:
            die("wrong backup password")
        body = _aes_cbc(master, iv, body, True)
    elif enc != "none":
        die(f"unknown backup encryption {enc!r}")
    if compressed:
        try:
            body = zlib.decompressobj().decompress(body)
        except zlib.error:
            die("the backup is damaged (it does not decompress) - was it cut short?")
    return head, body


def write_ab(tar, version=5, compressed=True, password=None):
    """tar bytes -> .ab bytes, encrypted the way Android does it when a password is given"""
    lines = [b"ANDROID BACKUP", str(version).encode(), b"1" if compressed else b"0"]
    body = zlib.compress(tar, 9) if compressed else tar
    if password:
        user_salt, ck_salt, rounds = secrets.token_bytes(64), secrets.token_bytes(64), 10000
        user_iv, iv, master = secrets.token_bytes(16), secrets.token_bytes(16), secrets.token_bytes(32)
        user_key = hashlib.pbkdf2_hmac("sha1", _pw_bytes(password, version), user_salt, rounds, 32)
        check = _checksum(master, ck_salt, rounds, version)
        blob = _aes_cbc(user_key, user_iv, bytes([16]) + iv + bytes([32]) + master + bytes([32]) + check, False)
        lines += [b"AES-256"] + [x.hex().upper().encode() for x in (user_salt, ck_salt)] + [str(rounds).encode()]
        lines += [user_iv.hex().upper().encode(), blob.hex().upper().encode()]
        body = _aes_cbc(master, iv, body, False)
    else:
        lines.append(b"none")
    return b"\n".join(lines) + b"\n" + body


def members(tar):
    """[(TarInfo, bytes or None)] in the archive's order - the order matters to Android (_manifest first)"""
    out = []
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as t:
        for m in t:
            out.append((m, t.extractfile(m).read() if m.isfile() else None))
    return out


def build_tar(items):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for info, data in items:
            info = _copy(info)
            if data is not None:
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            else:
                t.addfile(info)
    return buf.getvalue()


def _copy(info):
    new = tarfile.TarInfo(info.name)
    for k in ("size", "mtime", "mode", "type", "linkname", "uid", "gid", "uname", "gname"):
        setattr(new, k, getattr(info, k))
    return new


def packages(items):
    return sorted({m.name.split("/")[1] for m, _ in items if m.name.startswith("apps/") and m.name.count("/") >= 2})


def find(items, package, path):
    """the member apps/<package>/<path>"""
    name = f"apps/{package}/{path}"
    return next(((m, d) for m, d in items if m.name == name), (None, None))


def summary(items, package):
    files = [(m.name.split("/", 2)[2], m.size) for m, _ in items if m.isfile() and m.name.startswith(f"apps/{package}/")]
    by_domain = {}
    for name, size in files:
        d = name.split("/")[0]
        by_domain.setdefault(d, [0, 0])
        by_domain[d][0] += 1
        by_domain[d][1] += size
    return files, by_domain


DOMAINS = {"_manifest": "app manifest", "db": "databases", "f": "files", "sp": "settings", "r": "other",
           "a": "app", "ef": "external files", "c": "cache", "nb": "no-backup"}


def describe(items, package, out=print):
    pkgs = packages(items)
    out(f"apps:      {', '.join(pkgs) or 'none - the backup is empty'}")
    if package not in pkgs:
        return False
    files, by_domain = summary(items, package)
    for d, (n, size) in sorted(by_domain.items()):
        out(f"  {DOMAINS.get(d, d):<15} {n:>5} files  {size / 1e6:9.1f} MB")
    dbs = [n for n, _ in files if n.startswith("db/") and n[3:] in DBS]
    keys = [n for n, _ in files if n.startswith("f/") and n[2:] in KEYS]
    out(f"databases: {', '.join(n[3:] for n in dbs) or 'none'}")
    if keys:
        out(f"keys:      {', '.join(n[2:] for n in keys)}")
    return bool(dbs)


def load(path, password):
    if not os.path.exists(path):
        die(f"{path} not found")
    data = open(path, "rb").read()
    head, tar = read_ab(data, password)
    try:
        items = members(tar)
    except tarfile.TarError as e:
        die(f"the backup's archive is damaged: {e}")
    return head, items


def checked_db(path):
    """an edited database, folded into one file and integrity-checked"""
    if not os.path.exists(path):
        die(f"{path} not found")
    con = sqlite3.connect(path)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    if ok != "ok":
        die(f"{path} fails the integrity check: {ok}")
    data = open(path, "rb").read()
    if data[:16] != b"SQLite format 3\x00":
        die(f"{path} is not an SQLite database")
    return data


def repack(items, package, replace):
    """swap databases inside the backup; their stale -wal/-shm/-journal files are dropped so they can't undo the edit"""
    names = {f"apps/{package}/db/{n}": data for n, data in replace.items()}
    stale = {f"{n}{s}" for n in names for s in ("-wal", "-shm", "-journal")}
    missing = set(names) - {m.name for m, _ in items}
    if missing:
        die(f"the backup has no {', '.join(sorted(missing))} to replace")
    out, dropped = [], []
    for m, d in items:
        if m.name in stale:
            dropped.append(m.name.rsplit("/", 1)[1])
            continue
        out.append((m, names.get(m.name, d)))
    return out, dropped


# ---- the phone ------------------------------------------------------------------------------------------------

def parse_dumpsys(text, package):
    """`dumpsys package <package>` -> {version, code, target, flags}, from the package's own block"""
    start = text.find(f"Package [{package}]")
    if start < 0:
        return None
    block = text[start:]
    nxt = block.find("\n  Package [", 1)
    block = block if nxt < 0 else block[:nxt]
    info = {"version": None, "code": None, "target": None, "flags": set()}
    m = re.search(r"versionName=(\S+)", block)
    info["version"] = m.group(1) if m else None
    m = re.search(r"versionCode=(\d+)", block)
    info["code"] = int(m.group(1)) if m else None
    m = re.search(r"targetSdk=(\d+)", block)
    info["target"] = int(m.group(1)) if m else None
    m = re.search(r"^\s*flags=\[([^\]]*)\]", block, re.M)
    info["flags"] = set(m.group(1).split()) if m else set()
    return info


def need_device():
    if not shutil.which("adb"):
        die("needs adb (macOS: brew install android-platform-tools; Linux: apt install adb; "
            "Windows: Android SDK Platform-Tools)")
    state = adb("get-state", check=False).strip()
    if state != "device":
        die("no phone over adb - check `adb devices`, that USB debugging is on, and tap 'Allow' on the phone")
    return int(adb("shell", "getprop ro.build.version.sdk").strip() or 0)


def app_info(package):
    return parse_dumpsys(adb("shell", f"dumpsys package {package}", check=False), package)


def verdict(sdk, info):
    """(adb backup includes the app's data?, why not)"""
    if not info:
        return False, "not installed"
    if "ALLOW_BACKUP" not in info["flags"]:
        return False, "this WhatsApp opts out of adb backup (allowBackup=false)"
    if sdk >= 31 and (info["target"] or 0) >= 31:
        return False, f"Android 12+ leaves out apps built for Android 12+ (this one targets SDK {info['target']})"
    return True, ""


def apk_paths(package):
    return [l.split(":", 1)[1].strip() for l in adb("shell", f"pm path {package}", check=False).splitlines()
            if l.startswith("package:")]


def install(apks, downgrade=False, sdk=0):
    args = ["install-multiple" if len(apks) > 1 else "install", "-r"]
    if downgrade:
        args.append("-d")
    if sdk >= 34:
        args.append("--bypass-low-target-sdk-block")
    r = subprocess.run(["adb", *args, *apks], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    text = (r.stdout + r.stderr).strip()
    return r.returncode == 0 and "Failure" not in text, text


def saved_apks(folder):
    apks = sorted(glob.glob(os.path.join(folder, "*.apk")), key=lambda p: (os.path.basename(p) != "base.apk", p))
    if not apks:
        die(f"no .apk files in {folder}")
    return apks


# ---- commands -------------------------------------------------------------------------------------------------

def cmd_info(a):
    sdk = need_device()
    model = adb("shell", "getprop ro.product.model").strip()
    release = adb("shell", "getprop ro.build.version.release").strip()
    print(f"phone:     {model}, Android {release} (SDK {sdk})")
    info = app_info(a.package)
    if not info:
        print(f"whatsapp:  {a.package} is not installed")
        sys.exit(1)
    print(f"whatsapp:  {a.package} {info['version']} (code {info['code']}, targets SDK {info['target']})")
    print(f"apks:      {len(apk_paths(a.package))} file(s)")
    ok, why = verdict(sdk, info)
    if ok:
        print("adb backup: includes WhatsApp's data - go straight to `wakit adb-backup backup`")
        return
    print(f"adb backup: leaves WhatsApp out - {why}")
    print("next:      `wakit adb-backup downgrade` with an old WhatsApp APK that allows backups (e.g. 2.11.431)")
    if sdk >= 30:
        print("note:      Android 11+ often refuses the downgrade; if it does, wakit puts your WhatsApp back and "
              "the .crypt15 route (`wakit crypt`) is the no-root option left")
    if sdk >= 34:
        print("note:      Android 14+ blocks old apps by default; wakit installs with --bypass-low-target-sdk-block")


def cmd_downgrade(a):
    sdk = need_device()
    if not os.path.exists(a.apk):
        die(f"{a.apk} not found")
    info = app_info(a.package)
    if not info:
        die(f"{a.package} is not installed on the phone")
    ok, _ = verdict(sdk, info)
    if ok:
        print(f"{a.package} {info['version']} already allows adb backup - no downgrade needed")
        return
    paths = apk_paths(a.package)
    if not paths:
        die(f"can't find {a.package}'s APK files on the phone")
    print(f"installed: {a.package} {info['version']} ({len(paths)} APK files)")
    print("this uninstalls WhatsApp KEEPING its data, installs the old APK over that data, and leaves WhatsApp "
          "unusable until `wakit adb-backup upgrade`. Make a normal WhatsApp backup first.")
    if not a.yes:
        die("pass --yes to go ahead")
    os.makedirs(a.save_to, exist_ok=True)
    if glob.glob(os.path.join(a.save_to, "*.apk")):
        die(f"{a.save_to} already holds APK files - use an empty folder, so the saved WhatsApp is exactly this one")
    print(f"saving the installed WhatsApp into {a.save_to} ...")
    for p in paths:
        adb("pull", p, os.path.join(a.save_to, os.path.basename(p)))
    saved = saved_apks(a.save_to)
    if len(saved) != len(paths) or any(os.path.getsize(p) == 0 for p in saved):
        die("the APKs did not copy completely - nothing was changed on the phone")
    with open(os.path.join(a.save_to, "wakit-saved.txt"), "w") as f:
        f.write(f"{a.package} {info['version']} code {info['code']}\n")
    adb("shell", f"am force-stop {a.package}", check=False)
    out = adb("shell", f"pm uninstall -k {a.package}", check=False).strip()
    if "Success" not in out:
        die(f"uninstall (keeping data) failed: {out} - nothing was changed")
    print("installing the old WhatsApp over the kept data ...")
    ok, text = install([a.apk], downgrade=True, sdk=sdk)
    if not ok:
        print(f"the phone refused the old APK: {text}")
        print("putting your WhatsApp back ...")
        back, text2 = install(saved, sdk=sdk)
        if not back:
            die(f"reinstalling the saved WhatsApp failed too: {text2}\n"
                f"your chats are still on the phone (the data was kept): install WhatsApp from the Play Store, or "
                f"run `wakit adb-backup upgrade --apks {a.save_to}`")
        die("WhatsApp is back as it was. This phone won't take an older WhatsApp, so adb backup can't reach it; "
            "use the .crypt15 route instead (`wakit crypt`)")
    new = app_info(a.package)
    ok, why = verdict(sdk, new)
    print(f"installed: {a.package} {new and new['version']}")
    if not ok:
        print(f"warning: adb backup still leaves it out - {why}; this APK won't do. Run "
              f"`wakit adb-backup upgrade --apks {a.save_to}` to put your WhatsApp back")
        sys.exit(1)
    print("ready: run `wakit adb-backup backup`. Don't open WhatsApp until `upgrade`; airplane mode is safest.")


def cmd_backup(a):
    sdk = need_device()
    info = app_info(a.package)
    if not info:
        die(f"{a.package} is not installed on the phone")
    ok, why = verdict(sdk, info)
    if not ok:
        print(f"warning: {why} - the backup will most likely come out empty (run `downgrade` first)")
    if os.path.exists(a.out) and not a.force:
        die(f"{a.out} exists - pass --force to overwrite")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    adb("shell", f"am force-stop {a.package}", check=False)
    print("on the phone: unlock it, then tap 'Back up my data'.")
    print("  leave the password empty, or type one and pass the same --password to extract/pack later.")
    print("  some phones insist on a password (it's then your screen-lock PIN or password).")
    r = subprocess.run(["adb", "backup", "-f", a.out, "-noapk", a.package], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    msg = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if "deprecated" not in l).strip()
    if msg:
        print(msg)
    if r.returncode != 0 or not os.path.exists(a.out):
        die("adb backup failed")
    size = os.path.getsize(a.out)
    with open(a.out, "rb") as f:
        start = f.read(len(MAGIC))
    if start != MAGIC:
        die(f"the backup is empty ({size} bytes): the phone cancelled it, or the time to confirm ran out")
    if _encrypted(a.out) and not a.password:
        print(f"saved {a.out} ({size / 1e6:.1f} MB, password-protected): pass --password to check what's inside")
        if size < 4096:
            print("warning: that's very small - WhatsApp's data is most likely not in it")
        return
    _, items = load(a.out, a.password)
    print(f"saved {a.out} ({size / 1e6:.1f} MB)")
    if not describe(items, a.package):
        die(f"WhatsApp's databases are not in the backup: the phone left the app out ({why or 'see info'})")


def cmd_extract(a):
    head, items = load(a.input, a.password)
    print(f"backup:    {a.input} (version {head['version']}"
          f"{', password-protected' if head['encrypted'] else ''})")
    if not describe(items, a.package):
        die(f"no {a.package} databases in this backup")
    os.makedirs(a.out, exist_ok=True)
    wrote = []
    for name in DBS + [n + s for n in DBS for s in ("-wal", "-shm")]:
        _, data = find(items, a.package, f"db/{name}")
        if data is None:
            continue
        p = os.path.join(a.out, name)
        if os.path.exists(p) and not a.force and not name.endswith(("-wal", "-shm")):
            die(f"{p} exists - pass --force to overwrite")
        open(p, "wb").write(data)
        wrote.append(name)
    for name in DBS:  # fold each write-ahead log in, so every database is one self-contained file
        p = os.path.join(a.out, name)
        if os.path.exists(p):
            con = sqlite3.connect(p)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            for s in ("-wal", "-shm"):
                if os.path.exists(p + s):
                    os.remove(p + s)
    for name in KEYS:
        _, data = find(items, a.package, f"f/{name}")
        if data is not None:
            open(os.path.join(a.out, name), "wb").write(data)
            wrote.append(name)
    print(f"wrote:     {', '.join(n for n in wrote if not n.endswith(('-wal', '-shm')))} into {a.out}")
    if os.path.exists(os.path.join(a.out, "key")):
        print("tip:       `key` also decrypts this phone's .crypt14 backups: wakit crypt decrypt --key "
              f"{os.path.join(a.out, 'key')} ...")


def cmd_pack(a):
    replace = {n: checked_db(p) for n, p in (("msgstore.db", a.db), ("wa.db", a.wa_db),
                                             ("stickers.db", a.stickers_db)) if p}
    if not replace:
        die("give at least one edited database: --db, --wa-db or --stickers-db")
    head, items = load(a.input, a.password)
    if a.package not in packages(items):
        die(f"{a.input} holds no {a.package} data")
    new_items, dropped = repack(items, a.package, replace)
    if os.path.exists(a.out) and not a.force:
        die(f"{a.out} exists - pass --force to overwrite")
    out = write_ab(build_tar(new_items), head["version"], head["compressed"], a.password if head["encrypted"] else None)
    # self-check: read it back as Android would, and compare every file
    again = members(read_ab(out, a.password if head["encrypted"] else None)[1])
    if [(m.name, d) for m, d in again] != [(m.name, d) for m, d in new_items]:
        die("self-check failed - nothing written")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "wb").write(out)
    print(f"replaced:  {', '.join(replace)}" + (f" (dropped stale {', '.join(dropped)})" if dropped else ""))
    print(f"wrote:     {a.out} ({len(out) / 1e6:.1f} MB), checked by reading it back")
    print("next:      wakit adb-backup restore --in " + a.out)


def cmd_restore(a):
    head, items = load(a.input, a.password) if (a.password or not _encrypted(a.input)) else (None, None)
    if items is not None:
        if a.package not in packages(items) or find(items, a.package, "_manifest")[0] is None:
            die(f"{a.input} holds no {a.package} data to restore")
        _, db = find(items, a.package, "db/msgstore.db")
        if db is not None:
            with tempfile.TemporaryDirectory() as tmp:
                p = os.path.join(tmp, "m.db")
                open(p, "wb").write(db)
                checked_db(p)
    sdk = need_device()
    info = app_info(a.package)
    if not info:
        die(f"{a.package} is not installed - install the same old WhatsApp you backed up with, then restore")
    ok, why = verdict(sdk, info)
    if not ok:
        die(f"the installed WhatsApp would ignore the restore: {why}. Install the old WhatsApp first "
            f"(`wakit adb-backup downgrade`), restore, then `upgrade`.")
    print(f"restoring into {a.package} {info['version']}. This REPLACES all of WhatsApp's data on the phone "
          "with the backup's contents.")
    if not a.yes:
        die("pass --yes to restore")
    adb("shell", f"am force-stop {a.package}", check=False)
    print("on the phone: unlock it, type the backup's password if it has one, then tap 'Restore my data'.")
    r = subprocess.run(["adb", "restore", a.input], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    msg = "\n".join(l for l in (r.stdout + r.stderr).splitlines() if "deprecated" not in l).strip()
    if msg:
        print(msg)
    if r.returncode != 0:
        die("adb restore failed")
    print("restore sent. Wait for the phone to say it's finished, then: wakit adb-backup upgrade --apks <folder>")


def _encrypted(path):
    if not os.path.exists(path):
        die(f"{path} not found")
    with open(path, "rb") as f:
        head = f.read(64).split(b"\n")
    return len(head) > 3 and head[3] != b"none"


def cmd_upgrade(a):
    sdk = need_device()
    apks = saved_apks(a.apks)
    print(f"installing {len(apks)} APK file(s) from {a.apks} ...")
    ok, text = install(apks, sdk=sdk)
    if not ok:
        die(f"install failed: {text}\nyou can also update WhatsApp from the Play Store: the data stays")
    info = app_info(a.package)
    print(f"installed: {a.package} {info and info['version']}")
    if not a.no_launch:
        adb("shell", "input keyevent 224", check=False)
        adb("shell", f"monkey -p {a.package} -c android.intent.category.LAUNCHER 1", check=False)
    print("done: turn airplane mode off and open WhatsApp")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--package", default="com.whatsapp", help="app package (default: com.whatsapp; "
                                                            "com.whatsapp.w4b for WhatsApp Business)")
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("info", help="check the phone and whether adb backup can include WhatsApp")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("downgrade", help="save the installed WhatsApp, then install an old one that allows backups")
    s.add_argument("--apk", required=True, help="an old official WhatsApp APK that allows adb backup (e.g. 2.11.431)")
    s.add_argument("--save-to", required=True, help="empty folder to save the installed WhatsApp's APKs in")
    s.add_argument("--yes", action="store_true", help="confirm: WhatsApp is swapped for the old version (data kept)")
    s.set_defaults(fn=cmd_downgrade)
    s = sub.add_parser("backup", help="adb backup WhatsApp's data into a .ab file")
    s.add_argument("--out", required=True, help="the .ab file to write (whatsapp.ab)")
    s.add_argument("--password", help="the backup password, if you type one on the phone (to check the result)")
    s.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("extract", help="take the databases and key out of a .ab file")
    s.add_argument("--in", dest="input", required=True, help="the .ab file")
    s.add_argument("--out", required=True, help="folder to write msgstore.db, wa.db, stickers.db, key into")
    s.add_argument("--password", help="the backup password, if it has one")
    s.add_argument("--force", action="store_true", help="overwrite databases already in --out")
    s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("pack", help="put edited databases into a copy of the .ab file")
    s.add_argument("--in", dest="input", required=True, help="the original .ab file from `backup`")
    s.add_argument("--db", help="the edited msgstore.db")
    s.add_argument("--wa-db", help="an edited wa.db (optional)")
    s.add_argument("--stickers-db", help="an edited stickers.db (optional)")
    s.add_argument("--out", required=True, help="the new .ab file to write")
    s.add_argument("--password", help="the backup password, if it has one (the new file keeps it)")
    s.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    s.set_defaults(fn=cmd_pack)
    s = sub.add_parser("restore", help="adb restore a .ab file onto the phone")
    s.add_argument("--in", dest="input", required=True, help="the .ab file to restore (from `pack`)")
    s.add_argument("--password", help="the backup password, if it has one (to check the file first)")
    s.add_argument("--yes", action="store_true", help="confirm: WhatsApp's data on the phone is replaced")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("upgrade", help="reinstall the WhatsApp saved by downgrade")
    s.add_argument("--apks", required=True, help="the folder `downgrade --save-to` saved the APKs in")
    s.add_argument("--no-launch", action="store_true", help="do not start WhatsApp afterwards")
    s.set_defaults(fn=cmd_upgrade)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
