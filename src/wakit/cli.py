"""wakit command line: `wakit <command> [options]`, `wakit <command> -h` for a command's options."""
import importlib, sys

from . import __version__

# command -> (module, one-line summary, section)
COMMANDS = {
    "import": ("export_import", "add an \"Export chat\" .zip/.txt to an Android msgstore.db", "Android"),
    "merge": ("db_merge", "copy the history of one Android msgstore.db into another", "Android"),
    "crypt": ("crypt", "decrypt / re-encrypt .crypt14/.crypt15 backups (no root needed)", "Android"),
    "stickers": ("sticker_merge", "copy favourite/recent stickers between Android stickers.db files", "Android"),
    "from-ios": ("from_ios", "iPhone backup -> Android msgstore.db", "iPhone"),
    "stickers-from-ios": ("ios_stickers", "iPhone backup stickers -> Android stickers.db", "iPhone"),
    "to-ios": ("to_ios", "Android msgstore.db -> iPhone backup (restore it to the iPhone)", "iPhone"),
    "ios-backup": ("ios_backup", "inspect, extract, check/fix, make and restore iPhone backups", "iPhone"),
    "device": ("device", "pull/install WhatsApp databases and media on a rooted Android phone (adb)", "Devices"),
    "gui": ("gui", "open the wakit window in your browser (runs only on this computer)", "Devices"),
}


def module(cmd):
    return importlib.import_module(f".{COMMANDS[cmd][0]}", __package__)


def usage():
    out = [f"wakit {__version__} - move WhatsApp chat history between exports, Android and iPhone", "",
           "usage: wakit [--serial DEVICE] <command> [options]      (wakit <command> -h for details)", ""]
    for section in ("Android", "iPhone", "Devices"):
        out.append(f"{section}:")
        out += [f"  {c:<18} {s}" for c, (_, s, sec) in COMMANDS.items() if sec == section]
        out.append("")
    out += ["Everything is a dry run until you pass --apply, and every write makes a backup first.",
            "Docs: https://github.com/menaxmaged/wakit"]
    return "\n".join(out)


def main(argv=None):
    import os
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--serial"] and len(argv) > 1:  # which adb device, like adb -s
        os.environ["ANDROID_SERIAL"] = argv[1]
        argv = argv[2:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        sys.exit(f"wakit: unknown command '{cmd}'\n\n{usage()}")
    sys.argv[0] = f"wakit {cmd}"  # argparse names the program after argv[0]
    module(cmd).main(rest)


if __name__ == "__main__":
    main()
