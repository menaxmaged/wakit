"""Decrypt and re-encrypt WhatsApp's Android backup files (msgstore.db.crypt14 / .crypt15) - the no-root path.

  info      what a backup file is: format, app version, whether wakit can open it
  decrypt   .crypt14/.crypt15 -> a plain msgstore.db (or wa.db, stickers.db ...) that the other tools can edit
  encrypt   an edited plain database -> a .crypt14/.crypt15 that WhatsApp can restore from

Keys:
  .crypt15  the 64-digit key of an end-to-end encrypted backup (WhatsApp > Settings > Chats > Chat backup >
            End-to-end encrypted backup > use a 64-digit key), or the encrypted_backup.key file from a rooted phone
  .crypt14  the 158-byte `key` file from /data/data/com.whatsapp/files/key (needs root once, to copy it)

Backups are in /sdcard/Android/media/com.whatsapp/WhatsApp/Databases (older phones: /sdcard/WhatsApp/Databases),
which adb and file managers can read without root.

Restoring an edited backup: put the .crypt file in that Databases folder as the newest backup, reinstall (or clear
the data of) WhatsApp, verify your number and tap Restore. Experimental - keep the original file.

Format: [header length][0x01][protobuf header: version, IV, app info][AES-256-GCM(zlib(database) or a ZIP of it)]
[16-byte GCM tag][16-byte MD5 of everything before - missing in some backups].
crypt15 keys are HKDF-SHA256(root key, salt 0, info "backup encryption").

Needs the `cryptography` package: pip install "wakit[crypt]"   (or: pip install cryptography)

examples:
  wakit crypt info --in msgstore.db.crypt15
  wakit crypt decrypt --in msgstore.db.crypt15 --key "1a2b 3c4d ..." --out msgstore.db
  wakit crypt encrypt --in msgstore.db --like msgstore.db.crypt15 --key "1a2b 3c4d ..." --out new/msgstore.db.crypt15
"""
import argparse, hashlib, hmac, io, os, re, secrets, sys, zipfile, zlib


def die(msg):
    sys.exit(f"error: {msg}")


def aes_gcm():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        die('needs the cryptography package: pip install "wakit[crypt]"  (or: pip install cryptography)')
    return Cipher, algorithms, modes


def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def fields(b, base=0):
    """protobuf fields -> [(field, wire type, value, offset of value in the file)]"""
    out, i = [], 0
    while i < len(b):
        key, i = varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(b, i)
            out.append((f, wt, v, None))
        elif wt == 2:
            n, i = varint(b, i)
            out.append((f, wt, b[i:i + n], base + i))
            i += n
        elif wt == 1:
            out.append((f, wt, b[i:i + 8], base + i))
            i += 8
        elif wt == 5:
            out.append((f, wt, b[i:i + 4], base + i))
            i += 4
        else:
            raise ValueError("not a protobuf header")
    return out


def parse(data):
    """header of a .crypt14/.crypt15 file: kind, IV (and where it is), where the encrypted data starts, app version"""
    if len(data) < 64:
        die("file too small to be a WhatsApp backup")
    size, off = data[0], 1
    if data[1] == 1:  # "feature table present" flag
        off = 2
    head = data[off:off + size]
    try:
        top = fields(head, off)
    except (ValueError, IndexError):
        die("not a .crypt14/.crypt15 file (crypt12 and older are not supported)")
    kind = iv = iv_at = version = None
    for f, wt, v, at in top:
        if f == 3 and wt == 2:  # crypt15: {1: iv}
            for sf, swt, sv, sat in fields(v, at):
                if sf == 1 and swt == 2 and len(sv) == 16:
                    kind, iv, iv_at = "crypt15", sv, sat
        elif f == 2 and wt == 2:  # crypt14: {1: version, 2: key version, 3: server salt, 4: id salt, 5: iv}
            for sf, swt, sv, sat in fields(v, at):
                if sf == 5 and swt == 2 and len(sv) == 16:
                    kind, iv, iv_at = "crypt14", sv, sat
        elif f == 4 and wt == 2:  # app info: {1: "2.26.41.77", ...}
            for sf, swt, sv, _ in fields(v, at):
                if sf == 1 and swt == 2:
                    version = sv.decode(errors="replace")
    if not kind:
        die("no IV in the header - unsupported backup format")
    return {"kind": kind, "iv": iv, "iv_at": iv_at, "start": off + size, "version": version}


def root_key(text):
    """the 64-digit key -> 32 bytes"""
    digits = re.sub(r"[\s-]", "", text)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digits):
        return None
    return bytes.fromhex(digits)


def load_key(spec, kind):
    """--key: 64 hex digits, or a key file (crypt14 `key`, crypt15 `encrypted_backup.key`, raw 32 bytes, hex text)"""
    if os.path.isfile(os.path.expanduser(spec)):
        raw = open(os.path.expanduser(spec), "rb").read()
        if len(raw) == 158 and kind == "crypt14":  # Java-serialized key file; the AES key is its last 32 bytes
            return raw[126:158]
        if len(raw) == 32:
            key = raw
        elif root_key(raw.decode(errors="ignore")):
            key = root_key(raw.decode(errors="ignore"))
        elif raw[:2] == b"\xac\xed":  # Java-serialized byte[32] (encrypted_backup.key): length 32 then the bytes
            i = raw.find(b"\x00\x00\x00\x20")
            if i < 0 or len(raw) < i + 36:
                die(f"can't find the key inside {spec}")
            key = raw[i + 4:i + 36]
        else:
            die(f"{spec}: unknown key file")
    else:
        key = root_key(spec)
        if not key:
            die("--key must be the 64-digit backup key (hex, spaces allowed) or a key file")
    if kind == "crypt14":
        return key
    prk = hmac.new(b"\x00" * 32, key, hashlib.sha256).digest()  # HKDF-SHA256 extract, salt = zeros
    return hmac.new(prk, b"backup encryption\x01", hashlib.sha256).digest()  # expand, one block


def decrypt(data, key, head):
    """-> (database, packing): packing records the MD5 trailer and ZIP wrapping, so encrypt() can repeat them"""
    Cipher, algorithms, modes = aes_gcm()
    # most backups end in [GCM tag][MD5 of everything before]; some (stickers) end in just the tag
    md5 = hashlib.md5(data[:-16]).digest() == data[-16:]
    ct, tag = (data[head["start"]:-32], data[-32:-16]) if md5 else (data[head["start"]:-16], data[-16:])
    dec = Cipher(algorithms.AES(key), modes.GCM(head["iv"], tag)).decryptor()
    try:
        plain = dec.update(ct) + dec.finalize()
    except Exception:
        die("wrong key (or a damaged file): the backup did not decrypt")
    packing = {"md5": md5, "zip": None}
    if plain[:4] == b"PK\x03\x04":  # a ZIP holding the database (stickers.db backups)
        with zipfile.ZipFile(io.BytesIO(plain)) as z:
            names = z.namelist()
            if len(names) != 1:
                die(f"the backup holds {len(names)} files ({', '.join(names[:5])}) - only single-file backups are supported")
            packing["zip"] = names[0]
            return z.read(names[0]), packing
    try:
        return zlib.decompress(plain), packing
    except zlib.error:
        return plain, packing


def encrypt(db, key, template, packing):
    """a new backup file packed like the template: its header with a fresh IV, then the database"""
    Cipher, algorithms, modes = aes_gcm()
    head = parse(template)
    iv = secrets.token_bytes(16)  # never reuse an IV with the same key
    header = bytearray(template[:head["start"]])
    header[head["iv_at"]:head["iv_at"] + 16] = iv
    if packing["zip"]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(packing["zip"], db)
        payload = buf.getvalue()
    else:
        payload = zlib.compress(db)
    enc = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    out = bytes(header) + enc.update(payload) + enc.finalize() + enc.tag
    return out + hashlib.md5(out).digest() if packing["md5"] else out


def cmd_info(a):
    data = open(a.input, "rb").read()
    h = parse(data)
    print(f"file:     {a.input} ({len(data) / 1e6:.1f} MB)")
    print(f"format:   {h['kind']}" + (f", made by WhatsApp {h['version']}" if h["version"] else ""))
    print(f"checksum: {'MD5 ok' if hashlib.md5(data[:-16]).digest() == data[-16:] else 'none (checked by the GCM tag when decrypting)'}")
    print("key:      " + ("the 64-digit end-to-end backup key (or encrypted_backup.key)" if h["kind"] == "crypt15"
                          else "the `key` file from /data/data/com.whatsapp/files/key"))


def cmd_decrypt(a):
    data = open(a.input, "rb").read()
    h = parse(data)
    db, _ = decrypt(data, load_key(a.key, h["kind"]), h)
    if db[:16] != b"SQLite format 3\x00":
        print("note: the result is not an SQLite database (a settings/JSON backup?) - written as is")
    if os.path.exists(a.out) and not a.force:
        die(f"{a.out} exists - pass --force to overwrite")
    open(a.out, "wb").write(db)
    print(f"decrypted {a.input} ({h['kind']}) -> {a.out} ({len(db) / 1e6:.1f} MB)")


def cmd_encrypt(a):
    template = open(a.like, "rb").read()
    h = parse(template)
    key = load_key(a.key, h["kind"])
    _, packing = decrypt(template, key, h)  # proves the key is right, and shows how the file is packed
    db = open(a.input, "rb").read()
    if db[:16] != b"SQLite format 3\x00":
        die(f"{a.input} is not an SQLite database")
    out = encrypt(db, key, template, packing)
    if decrypt(out, key, parse(out))[0] != db:
        die("self-check failed - nothing written")
    if os.path.exists(a.out) and not a.force:
        die(f"{a.out} exists - pass --force to overwrite")
    open(a.out, "wb").write(out)
    print(f"encrypted {a.input} -> {a.out} ({h['kind']}, {len(out) / 1e6:.1f} MB), checked by decrypting it again")
    print("restore: copy it into WhatsApp/Databases on the phone as the newest backup, reinstall WhatsApp, Restore")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("info", help="describe a .crypt14/.crypt15 backup")
    s.add_argument("--in", dest="input", required=True, help="the backup file (msgstore.db.crypt15 ...)")
    s.set_defaults(fn=cmd_info)
    s = sub.add_parser("decrypt", help="backup file -> plain database")
    s.add_argument("--in", dest="input", required=True, help="the backup file (msgstore.db.crypt15 ...)")
    s.add_argument("--key", required=True, help="the 64-digit backup key, or a key file")
    s.add_argument("--out", required=True, help="the plain database to write (msgstore.db)")
    s.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    s.set_defaults(fn=cmd_decrypt)
    s = sub.add_parser("encrypt", help="plain database -> backup file WhatsApp can restore")
    s.add_argument("--in", dest="input", required=True, help="the plain database (msgstore.db)")
    s.add_argument("--like", required=True, help="an original backup file made with the same key (header template)")
    s.add_argument("--key", required=True, help="the 64-digit backup key, or a key file")
    s.add_argument("--out", required=True, help="the backup file to write (msgstore.db.crypt15)")
    s.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    s.set_defaults(fn=cmd_encrypt)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
