# Changelog

## 0.3.0 (2026-10-27)
- `wakit doctor`: identifies a database, iPhone backup, `.crypt14`/`.crypt15` file or chat export, shows what's in it, and checks its layout. Every command that writes now runs the same check first and refuses an unknown layout, without changing anything.
- Databases decrypted from `.crypt14`/`.crypt15` backups have no search index (`message_ftsv2`). `import`, `from-ios` and `merge` now skip it instead of failing, and WhatsApp rebuilds it after the restore. This fixes the no-root workflow.
- Output piped into `head` and the like, and Ctrl+C, now end quietly instead of printing a traceback.

## 0.2.0 (2026-10-26)
- Standalone apps for macOS, Windows and Linux, attached to each release. There's no Python to install; double-click to open the GUI.
- `src/` layout. Issue templates, contributing and security notes.

## 0.1.0 (2026-10-26)
First release.
- `import`: "Export chat" .zip/.txt (iPhone and Android formats) → Android `msgstore.db`, with media.
- `merge`, `stickers`: one Android database into another.
- `from-ios` / `stickers-from-ios`: iPhone backup → Android.
- `to-ios`: Android → iPhone backup.
- `ios-backup`: info, extract, check `--fix`, backup and restore for unencrypted iPhone backups.
- `device`: pull and install WhatsApp's databases and media on a rooted phone over adb.
- `crypt`: decrypt and re-encrypt `.crypt14` / `.crypt15` backups (no root).
- `gui`: a local web GUI with guides.
