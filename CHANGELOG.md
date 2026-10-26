# Changelog

## Unreleased
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
