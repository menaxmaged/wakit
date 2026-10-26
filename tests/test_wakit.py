"""Tests on synthetic data only: sample exports, a fake iPhone backup, the CLI and GUI plumbing."""
import base64, contextlib, io, os, plistlib, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from wakit import cli, crypt, export_import, gui, ios_backup, to_ios  # noqa: E402

try:
    import cryptography  # noqa: F401  (optional: only `wakit crypt` needs it)
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

IOS_EXPORT = """\
[03/01/2024, 9:15:02 PM] Alex: ‎Messages and calls are end-to-end encrypted. Only people in this chat can read, listen to, or share them.
[03/01/2024, 9:15:10 PM] Alex: hey!
[03/01/2024, 9:16:00 PM] Sam: hi
how are you?
‎[03/01/2024, 9:17:30 PM] Sam: ‎<attached: 00000005-PHOTO-2024-01-03-21-17-30.jpg>
[03/01/2024, 9:18:00 PM] Alex: ‎This message was deleted.
"""
ANDROID_EXPORT = """\
1/3/24, 21:15 - Messages and calls are end-to-end encrypted. Only people in this chat can read, listen to, or share them.
1/3/24, 21:15 - Alex: hey!
1/3/24, 21:16 - Sam: hi
1/3/24, 21:17 - Sam: <Media omitted>
"""


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def pb_decode(b):
    """tiny protobuf reader for the blobs to_ios writes: {field: [values]}"""
    out, i = {}, 0
    while i < len(b):
        key, i = varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(b, i)
        else:
            n, i = varint(b, i)
            v, i = b[i:i + n], i + n
        out.setdefault(f, []).append(v)
    return out


def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


class ExportParsing(unittest.TestCase):
    def test_ios_export(self):
        with tempfile.TemporaryDirectory() as d:
            msgs, fmt = export_import.parse_lines(write(os.path.join(d, "_chat.txt"), IOS_EXPORT))
        self.assertEqual(fmt, "ios")
        self.assertEqual({m["sender"] for m in msgs}, {"Alex", "Sam"})
        multi = [m for m in msgs if m["body"].startswith("hi")]
        self.assertEqual(multi[0]["body"], "hi\nhow are you?")  # continuation lines join the message

    def test_android_export(self):
        with tempfile.TemporaryDirectory() as d:
            msgs, fmt = export_import.parse_lines(write(os.path.join(d, "chat.txt"), ANDROID_EXPORT))
        self.assertEqual(fmt, "android")
        self.assertIn("hey!", [m["body"] for m in msgs])

    def test_system_lines(self):
        for line in ["Messages and calls are end-to-end encrypted.", "You blocked this person",
                     "Missed voice call", "Waiting for this message", "Sam is a contact."]:
            self.assertTrue(export_import.SYSTEM.search(line), line)
        self.assertFalse(export_import.SYSTEM.search("see you at the call center"))

    def test_date_order(self):
        msgs = [{"date": "25/12/2023"}, {"date": "03/01/2024"}]
        self.assertEqual(export_import.detect_date_order(msgs, "auto"), "dmy")
        self.assertEqual(export_import.detect_date_order([{"date": "12/25/23"}], "auto"), "mdy")


class IosBlobs(unittest.TestCase):
    def test_quote_blob(self):
        blob = to_ios.quote_blob({"key": "ABC123", "sender": "123@lid", "text": "hello"})
        d = pb_decode(blob)
        self.assertEqual(d[5], [b"ABC123"])
        self.assertEqual(d[6], [b"123@lid"])
        self.assertEqual(pb_decode(d[19][0])[1], [b"hello"])

    def test_push_blob(self):
        sent = pb_decode(base64.b64decode(to_ios.push_blob(1, 1700000000.5, "999@lid")))
        self.assertEqual(sent[1], [1700000000])
        self.assertEqual(sent[3], [b"999@lid"])
        got = pb_decode(base64.b64decode(to_ios.push_blob(0, 1700000000.5, "999@lid")))
        self.assertEqual(got[43], [1700000000500000])

    def test_file_id(self):
        # iOS names backup files sha1("<domain>-<path>")
        import hashlib
        rel = "Message/Media/1@lid/a/b/x.jpg"
        want = hashlib.sha1(f"{to_ios.DOMAIN}-{rel}".encode()).hexdigest()
        self.assertEqual(to_ios.file_id(rel), want)


def fake_backup(root, info_header=True):
    """a minimal unencrypted backup: Manifest.plist, Info.plist, Manifest.db with three file records"""
    os.makedirs(root)
    plistlib.dump({"IsEncrypted": False, "Lockdown": {"DeviceName": "Test iPhone", "ProductVersion": "18.0"}},
                  open(os.path.join(root, "Manifest.plist"), "wb"))
    info = plistlib.dumps({"Device Name": "Test iPhone", "Product Version": "18.0"})
    if not info_header:
        info = info.split(b"\n", 1)[1]  # like some third-party tools: no <?xml?> line
    open(os.path.join(root, "Info.plist"), "wb").write(info)
    man = sqlite3.connect(os.path.join(root, "Manifest.db"))
    man.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")

    def record(rel, size):
        return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)},
                               "$objects": ["$null", {"Size": size, "Mode": 33188, "RelativePath": plistlib.UID(2),
                                                      "$class": plistlib.UID(3)}, rel,
                                            {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]}]},
                              fmt=plistlib.FMT_BINARY)

    for fid, rel, data, size in [("aa" + "0" * 38, "good.txt", b"hello", 5), ("bb" + "0" * 38, "wrong.txt", b"abc", 99),
                                 ("cc" + "0" * 38, "gone.txt", None, 4)]:
        if data is not None:
            os.makedirs(os.path.join(root, fid[:2]), exist_ok=True)
            open(os.path.join(root, fid[:2], fid), "wb").write(data)
        man.execute("INSERT INTO Files VALUES(?,?,?,1,?)", (fid, ios_backup.WA_SHARED, rel, record(rel, size)))
    man.commit()
    man.close()


class Backups(unittest.TestCase):
    def test_check_and_fix(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "backup")
            fake_backup(root, info_header=False)
            missing, wrong = ios_backup.check(root)
            self.assertEqual([r for _, _, r in missing], ["gone.txt"])
            self.assertIn((ios_backup.WA_SHARED, "wrong.txt"), wrong)
            self.assertTrue(any(d_ == "Info.plist" for d_, _ in wrong))
            ios_backup.check(root, fix=True)
            self.assertEqual(ios_backup.check(root), ([], []))
            self.assertEqual(ios_backup.manifest_info(root)["device"], "Test iPhone")

    def test_extract(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = os.path.join(d, "backup"), os.path.join(d, "out")
            fake_backup(root)
            self.assertEqual(ios_backup.extract(root, out), 2)
            self.assertEqual(open(os.path.join(out, "good.txt"), "rb").read(), b"hello")

    def test_headerless_plist(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Info.plist")
            open(p, "wb").write(plistlib.dumps({"a": 1}).split(b"\n", 1)[1])
            self.assertEqual(ios_backup.read_plist(p), {"a": 1})


def crypt15_template(iv=b"\x11" * 16):
    """the start of a .crypt15 file: [header length][0x01][protobuf {1: 0, 3: {1: iv}, 4: {1: "2.26.0"}}]"""
    pb = b"\x08\x00" + b"\x1a\x12\x0a\x10" + iv + b"\x22\x08\x0a\x062.26.0"
    return bytes([len(pb), 1]) + pb


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package")
class Crypt(unittest.TestCase):
    KEY = "00112233445566778899aabbccddeeff" * 2
    DB = b"SQLite format 3\x00" + bytes(range(256)) * 40

    def test_key_derivation_is_hkdf(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        want = HKDF(hashes.SHA256(), 32, salt=None, info=b"backup encryption").derive(bytes.fromhex(self.KEY))
        self.assertEqual(crypt.load_key(self.KEY, "crypt15"), want)
        spaced = " ".join(self.KEY[i:i + 4] for i in range(0, 64, 4))  # as WhatsApp shows it
        self.assertEqual(crypt.load_key(spaced, "crypt15"), want)

    def test_crypt15_round_trip(self):
        key = crypt.load_key(self.KEY, "crypt15")
        packing = {"md5": True, "zip": None}
        first = crypt.encrypt(self.DB, key, crypt15_template() + b"\x00" * 48, packing)
        head = crypt.parse(first)
        self.assertEqual(head["kind"], "crypt15")
        self.assertEqual(head["version"], "2.26.0")
        self.assertNotEqual(head["iv"], b"\x11" * 16)  # a fresh IV every time
        self.assertEqual(crypt.decrypt(first, key, head), (self.DB, packing))
        again = crypt.encrypt(self.DB, key, first, packing)
        self.assertNotEqual(crypt.parse(again)["iv"], head["iv"])
        self.assertEqual(crypt.decrypt(again, key, crypt.parse(again))[0], self.DB)

    def test_zip_and_tag_only_packing(self):
        key = crypt.load_key(self.KEY, "crypt15")
        packing = {"md5": False, "zip": "stickers.db"}
        out = crypt.encrypt(self.DB, key, crypt15_template() + b"\x00" * 48, packing)
        self.assertEqual(crypt.decrypt(out, key, crypt.parse(out)), (self.DB, packing))

    def test_wrong_key(self):
        out = crypt.encrypt(self.DB, crypt.load_key(self.KEY, "crypt15"), crypt15_template() + b"\x00" * 48,
                            {"md5": True, "zip": None})
        with self.assertRaises(SystemExit):
            crypt.decrypt(out, crypt.load_key("ff" * 32, "crypt15"), crypt.parse(out))


class Plumbing(unittest.TestCase):
    def test_every_command_builds(self):
        for name in cli.COMMANDS:
            self.assertTrue(cli.module(name).build_parser().format_help())

    def test_usage(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main([])
        for name in cli.COMMANDS:
            self.assertIn(name, buf.getvalue())

    def test_gui_schema_and_argv(self):
        s = gui.schema()
        names = {c["name"] for c in s["commands"]}
        self.assertIn("to-ios", names)
        self.assertNotIn("gui", names)
        argv = gui.build_argv({"cmd": "import", "serial": "1.2.3.4:5555",
                               "args": {"db": "~/m.db", "export": "chat.zip", "contact": "Alex", "apply": True}})
        self.assertEqual(argv[:3], ["--serial", "1.2.3.4:5555", "import"])
        self.assertIn(os.path.expanduser("~/m.db"), argv)
        self.assertIn("--apply", argv)
        # options left empty fall back to the command's own defaults
        self.assertEqual(gui.build_argv({"cmd": "device", "sub": "pull", "args": {"out": "x"}}),
                         ["device", "pull", "--out", "x"])
        self.assertEqual(gui.build_argv({"cmd": "device", "sub": "info", "args": {"package": "com.whatsapp.w4b"}}),
                         ["device", "--package", "com.whatsapp.w4b", "info"])
        with self.assertRaises(ValueError):
            gui.build_argv({"cmd": "import", "args": {}})


if __name__ == "__main__":
    unittest.main()
