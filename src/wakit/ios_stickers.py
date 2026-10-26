"""Copy your own stickers (favorites and recents) from an iPhone WhatsApp backup into Android's stickers.db.

Reads Sticker.sqlite and stickers/no-sticker-pack/ from the backup folder. Favorites on the iPhone become
favorites (starred_stickers); the other saved stickers become recents. Sticker files are written to --files-out
named the way Android expects (files/Stickers/<sha256 base64, "/" as "->.webp); copy that folder's contents
into the app's files/Stickers. Store packs are not copied - re-download them from the sticker store.
Stickers already in the db are skipped. Dry run by default; pass --apply to write.

example:
  wakit stickers-from-ios --db stickers.db --backup ios/ --files-out Stickers --apply
"""
from . import ios_backup
import argparse, base64, os, shutil, sqlite3, subprocess, sys, time

FAVORITE = 1  # ZWACDSTICKER.ZFLAGS bit for a favorite


def size(path):
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path], capture_output=True, text=True).stdout
        w, h = (int(x) for x in out.split()[-3::2])
        return w, h
    except Exception:
        return 512, 512


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", required=True, help="Android stickers.db")
    p.add_argument("--backup", required=True, help="iPhone backup folder with Sticker.sqlite and stickers/, or a full unencrypted iPhone backup (holds Manifest.db)")
    p.add_argument("--files-out", required=True, help="folder to write the sticker files into")
    p.add_argument("--apply", action="store_true", help="actually write (otherwise dry run)")
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    a.backup = ios_backup.resolve(a.backup)

    ios = sqlite3.connect(f"file:{os.path.join(a.backup, 'Sticker.sqlite')}?immutable=1", uri=True)
    rows = ios.execute("""SELECT ZFLAGS, ZFILEHASH, ZENCFILEHASH, ZMEDIAKEY, ZDIRECTPATH, ZURL, ZMIMETYPE, ZEMOJIS,
                                 ZACCESSIBILITYTEXT, ZRELATIVEIMAGEPATH
                          FROM ZWACDSTICKER WHERE ZSTICKERPACK IS NULL ORDER BY ZSORT DESC, Z_PK DESC""").fetchall()
    con = sqlite3.connect(a.db)
    have = {h for (h,) in con.execute("SELECT plaintext_hash FROM starred_stickers UNION SELECT plaintext_hash FROM recent_stickers")}
    now, fav, rec, skipped = int(time.time() * 1000), 0, 0, 0
    os.makedirs(a.files_out, exist_ok=True)
    cur = con.cursor()
    cur.execute("BEGIN")
    for i, (flags, fhash, ehash, mkey, dpath, url, mime, emojis, alt, rel) in enumerate(rows):
        src = os.path.join(a.backup, "stickers", rel or "")
        if not fhash or not os.path.isfile(src) or fhash in have:
            skipped += 1
            continue
        w, h = size(src)
        common = {"plaintext_hash": fhash, "url": url, "enc_hash": ehash, "direct_path": dpath, "mimetype": mime or "image/webp",
                  "media_key": base64.b64encode(mkey).decode() if mkey else None, "file_size": os.path.getsize(src),
                  "width": w, "height": h, "emojis": emojis, "is_first_party": 0, "accessibility_text": alt}
        if (flags or 0) & FAVORITE:
            vals = {**common, "timestamp": now - i * 1000, "is_avatar": 0}
            table, fav = "starred_stickers", fav + 1
        else:
            vals = {**common, "entry_weight": float(len(rows) - i), "is_avocado": 0, "last_sticker_sent_ts": now - i * 1000}
            table, rec = "recent_stickers", rec + 1
        cur.execute(f'INSERT INTO {table}({",".join(vals)}) VALUES({",".join("?" * len(vals))})', list(vals.values()))
        if a.apply:
            shutil.copy2(src, os.path.join(a.files_out, fhash.replace("/", "-") + ".webp"))
    print(f"stickers: {fav} favorites, {rec} recents, {skipped} skipped (already there or file missing)")
    if not a.apply:
        con.rollback()
        print("dry run - nothing written. Re-run with --apply.")
        return
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"files:    {len(os.listdir(a.files_out))} in {a.files_out}")


if __name__ == "__main__":
    main()
