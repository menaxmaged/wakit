"""Copy favorite and recent stickers from another Android WhatsApp's stickers.db (+ its files/Stickers) into this one.

Rows already in the target (same plaintext_hash) are skipped, except that a favorite in the source which the
target only has as a recent is moved to the target's favorites. Sticker files are written to --files-out under the
name Android expects (<plaintext_hash with "/" as "->.webp); copy that folder's contents into the app's
files/Stickers. A row whose file is missing is still copied when it has a download path (the app fetches it).
Dry run by default; pass --apply to write.

example:
  wakit stickers --db stickers.db --from old/databases/stickers.db --from-files old/files/Stickers \\
                      --files-out Stickers --apply
"""
import argparse, os, shutil, sqlite3, sys

TABLES = ("starred_stickers", "recent_stickers")


def cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="target stickers.db")
    p.add_argument("--from", dest="src", required=True, help="source stickers.db")
    p.add_argument("--from-files", required=True, help="source files/Stickers folder")
    p.add_argument("--files-out", required=True, help="folder to write the sticker files into")
    p.add_argument("--apply", action="store_true", help="actually write (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)

    con = sqlite3.connect(a.db)
    src = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
    have = {h for t in TABLES for (h,) in con.execute(f"SELECT plaintext_hash FROM {t}")}
    recents = {h for (h,) in con.execute("SELECT plaintext_hash FROM recent_stickers")}
    os.makedirs(a.files_out, exist_ok=True)
    cur = con.cursor()
    cur.execute("BEGIN")
    counts, files, skipped, promoted = {}, 0, 0, 0
    for t in TABLES:
        common = [c for c in cols(src, t) if c in set(cols(con, t))]
        for row in src.execute(f'SELECT {",".join(common)} FROM {t}').fetchall():
            r = dict(zip(common, row))
            h = r["plaintext_hash"]
            name = h.replace("/", "-") + ".webp"
            f = os.path.join(a.from_files, name)
            if t == "starred_stickers" and h in recents:
                cur.execute("DELETE FROM recent_stickers WHERE plaintext_hash=?", (h,))
                cur.execute(f'INSERT INTO {t}({",".join(common)}) VALUES({",".join("?" * len(common))})', row)
                recents.discard(h)
                promoted += 1
                continue
            if h in have or (not os.path.isfile(f) and not r.get("direct_path")):
                skipped += 1
                continue
            cur.execute(f'INSERT INTO {t}({",".join(common)}) VALUES({",".join("?" * len(common))})', row)
            have.add(h)
            counts[t] = counts.get(t, 0) + 1
            if os.path.isfile(f):
                files += 1
                if a.apply:
                    shutil.copy2(f, os.path.join(a.files_out, name))
    print(f"stickers: {counts.get('starred_stickers', 0)} favorites, {counts.get('recent_stickers', 0)} recents "
          f"({files} with files), {promoted} recents made favorites, {skipped} skipped (already there, or no file "
          f"and no download path)")
    if not a.apply:
        con.rollback()
        print("dry run - nothing written. Re-run with --apply.")
        return
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


if __name__ == "__main__":
    main()
