"""Move WhatsApp's databases and media between a rooted Android phone and this computer, over adb.

  info        is the phone reachable, rooted, and where is WhatsApp's data
  pull        copy msgstore.db, wa.db and stickers.db off the phone (WhatsApp is disabled while copying)
  install     put an edited msgstore.db back, with the app's owner and SELinux label, then start WhatsApp
  enable      re-enable WhatsApp after a pull you do not follow with an install
  pull-media  copy the phone's WhatsApp/Media folder here (one tar stream: far faster than adb pull)
  push-media  copy a Media/ folder (made by `wakit import` / `wakit from-ios`) onto the phone

Needs adb, a phone with USB (or Wi-Fi) debugging on, and root (su) for the databases.

Why the dance: WhatsApp restarts itself seconds after a force-stop, and if it opens a half-copied database it
offers to "restore chat history". So the app is disabled (pm disable) for the whole pull/install, the new file is
copied next to the old one and renamed into place, and its owner and SELinux label are copied from the old file -
otherwise the app reports "something went wrong with chat history".

examples:
  wakit device info
  wakit device pull --out phone/
  wakit device install --db phone/msgstore.db
  wakit device pull-media --out phone-media/
  wakit --serial 192.168.1.20:5555 device info
"""
import argparse, hashlib, os, shlex, shutil, sqlite3, subprocess, sys, time

TMP = "/sdcard/wakit-tmp"
FILES = ["msgstore.db", "msgstore.db-wal", "msgstore.db-shm", "wa.db", "wa.db-wal", "wa.db-shm",
         "stickers.db", "stickers.db-wal", "stickers.db-shm"]


def die(msg):
    sys.exit(f"error: {msg}")


def adb(*args, check=True, stdin=None):
    if not shutil.which("adb"):
        die("needs adb (macOS: brew install android-platform-tools; Linux: apt install adb)")
    r = subprocess.run(["adb", *args], capture_output=True, text=True, stdin=stdin or subprocess.DEVNULL)
    if check and r.returncode != 0:
        die(f"adb {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def su(cmd, check=True):
    """run a shell command as root on the phone"""
    return adb("shell", f"su -c {shlex.quote(cmd)}", check=check)


def need_phone():
    if adb("get-state", check=False).strip() != "device":
        die("no phone over adb - check `adb devices`, USB debugging, and the 'Allow' prompt on the phone")
    if su("id", check=False).find("uid=0") < 0:
        die("no root: `su` did not give uid 0 (root the phone, and allow the shell in the su app)")


def data_dir(package):
    return f"/data/data/{package}/databases"


def owner_and_label(package):
    """(uid:gid, SELinux context) of the app's current msgstore.db"""
    d = data_dir(package)
    line = su(f"ls -lZ {d}/msgstore.db").strip().split()
    if len(line) < 5:
        die(f"no {d}/msgstore.db - is {package} installed and set up?")
    owner = f"{line[2]}:{line[3]}"
    label = next((f for f in line if f.startswith("u:object_r:")), None)
    return owner, label


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_info(a):
    need_phone()
    model = adb("shell", "getprop ro.product.model").strip()
    android = adb("shell", "getprop ro.build.version.release").strip()
    print(f"phone:    {model}, Android {android}")
    print("root:     yes")
    pkgs = [l.split(":", 1)[1] for l in adb("shell", "pm list packages").split() if "whatsapp" in l.lower()]
    print(f"whatsapp: {', '.join(pkgs) or 'not installed'}")
    if a.package in pkgs:
        owner, label = owner_and_label(a.package)
        print(f"data:     {data_dir(a.package)} (owner {owner}, label {label})")
        print(su(f"ls -la {data_dir(a.package)} | grep -E 'msgstore|wa.db|stickers'").rstrip())
        print(f"media:    {adb('shell', 'du -sh /sdcard/WhatsApp/Media 2>/dev/null', check=False).strip() or '?'}")


def cmd_pull(a):
    need_phone()
    os.makedirs(a.out, exist_ok=True)
    d = data_dir(a.package)
    print(f"disabling {a.package} while copying ...")
    su(f"pm disable {a.package}")
    time.sleep(2)
    present = [f for f in FILES if su(f"[ -e {d}/{f} ] && echo y", check=False).strip() == "y"]
    su(f"rm -rf {TMP} && mkdir -p {TMP} && " + " && ".join(f"cp {d}/{f} {TMP}/" for f in present))
    for f in present:
        adb("pull", f"{TMP}/{f}", os.path.join(a.out, f))
    su(f"rm -rf {TMP}")
    # fold the pulled WAL into the database, so the copy is one self-contained file
    for db in ("msgstore.db", "wa.db", "stickers.db"):
        p = os.path.join(a.out, db)
        if os.path.exists(p):
            con = sqlite3.connect(p)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
    print(f"pulled {', '.join(present)} into {a.out}")
    if a.enable:
        su(f"pm enable {a.package}")
        print(f"{a.package} enabled again")
    else:
        print(f"{a.package} stays disabled so no new messages are missed; run `wakit device install` "
              f"(or `wakit device enable`) when done")


def cmd_install(a):
    need_phone()
    if not os.path.exists(a.db):
        die(f"{a.db} not found")
    con = sqlite3.connect(a.db)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    if ok != "ok":
        die(f"{a.db} fails the integrity check: {ok}")
    d, name = data_dir(a.package), os.path.basename(a.target)
    owner, label = owner_and_label(a.package)
    su(f"pm disable {a.package}")
    time.sleep(2)
    local = md5(a.db)
    su(f"rm -rf {TMP} && mkdir -p {TMP}")
    print("copying to the phone ...")
    adb("push", a.db, f"{TMP}/new.db")
    remote = su(f"md5sum {TMP}/new.db", check=False).split()
    if remote and remote[0] != local:
        die("the copy on the phone does not match - nothing was replaced")
    steps = [f"cp {TMP}/new.db {d}/{name}.tmp", f"chown {owner} {d}/{name}.tmp", f"chmod 600 {d}/{name}.tmp"]
    if label:
        steps.append(f"chcon {label} {d}/{name}.tmp")
    steps += [f"rm -f {d}/{name}-wal {d}/{name}-shm", f"mv {d}/{name}.tmp {d}/{name}", f"rm -rf {TMP}"]
    su(" && ".join(steps))
    print(su(f"ls -lZ {d}/{name}").strip())
    su(f"pm enable {a.package}")
    if not a.no_launch:
        adb("shell", "input keyevent 224", check=False)  # wake the screen: a locked phone will not open the app
        adb("shell", f"monkey -p {a.package} -c android.intent.category.LAUNCHER 1", check=False)
    print(f"installed {a.db} as {d}/{name} and started {a.package}")


def cmd_enable(a):
    need_phone()
    su(f"pm enable {a.package}")
    print(f"{a.package} enabled")


def cmd_pull_media(a):
    if adb("get-state", check=False).strip() != "device":
        die("no phone over adb")
    os.makedirs(a.out, exist_ok=True)
    parent, folder = a.root.rstrip("/").rsplit("/", 1)
    print(f"streaming {a.root} into {a.out} (can take a while for a few GB) ...")
    src = subprocess.Popen(["adb", "exec-out", f"tar -cf - -C {shlex.quote(parent)} {shlex.quote(folder)}"],
                           stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    rc = subprocess.call(["tar", "-xf", "-", "-C", a.out], stdin=src.stdout)
    src.wait()
    if rc or src.returncode:
        die("the media copy failed")
    n = sum(len(f) for _, _, f in os.walk(a.out))
    print(f"copied {n} files into {os.path.join(a.out, folder)}")


def cmd_push_media(a):
    if adb("get-state", check=False).strip() != "device":
        die("no phone over adb")
    media = a.src if os.path.basename(a.src.rstrip("/")) == "Media" else os.path.join(a.src, "Media")
    if not os.path.isdir(media):
        die(f"no Media folder in {a.src}")
    print(f"copying {media} into {a.root}/ ...")
    adb("push", media, a.root.rstrip("/") + "/")
    print("done")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--package", default="com.whatsapp", help="app package (default: com.whatsapp; "
                                                            "com.whatsapp.w4b for WhatsApp Business)")
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("info", help="check the phone, root and WhatsApp's files")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("pull", help="copy WhatsApp's databases off the phone")
    s.add_argument("--out", required=True, help="folder to put msgstore.db, wa.db, stickers.db in")
    s.add_argument("--enable", action="store_true", help="re-enable WhatsApp right after copying")
    s.set_defaults(fn=cmd_pull)
    s = sub.add_parser("install", help="put an edited msgstore.db back and start WhatsApp")
    s.add_argument("--db", required=True, help="the msgstore.db to install")
    s.add_argument("--target", default="msgstore.db", help="file name to replace (default: msgstore.db)")
    s.add_argument("--no-launch", action="store_true", help="do not start WhatsApp afterwards")
    s.set_defaults(fn=cmd_install)
    s = sub.add_parser("enable", help="re-enable WhatsApp")
    s.set_defaults(fn=cmd_enable)
    s = sub.add_parser("pull-media", help="copy the phone's WhatsApp media here")
    s.add_argument("--out", required=True, help="folder to copy into (gets Media/ inside)")
    s.add_argument("--root", default="/sdcard/WhatsApp/Media", help="media folder on the phone "
                   "(default: /sdcard/WhatsApp/Media; newer phones: /sdcard/Android/media/com.whatsapp/WhatsApp/Media)")
    s.set_defaults(fn=cmd_pull_media)
    s = sub.add_parser("push-media", help="copy a Media/ folder onto the phone")
    s.add_argument("--src", required=True, help="folder holding Media/ (or the Media folder itself)")
    s.add_argument("--root", default="/sdcard/WhatsApp", help="the phone folder that holds Media/ "
                                                                "(default: /sdcard/WhatsApp)")
    s.set_defaults(fn=cmd_push_media)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
