# Changelog

## Unreleased
- `wakit adb-backup` (experimental): the no-root route over `adb backup` / `adb restore`. `info` says whether adb backup can reach WhatsApp. `downgrade` saves the installed WhatsApp and puts an old one that allows backups over the kept data, and puts yours straight back if the phone refuses. `backup`, `extract`, `pack` and `restore` handle the `.ab` file, password-protected ones included. `upgrade` reinstalls your WhatsApp.
- `doctor` reads `.ab` adb backups.
- GUI redesign: guided workflows with a working folder that fills in every path, step ticks and resume, plain-language field names with extra options folded away, a connected-phone indicator, clearer run results, and confirmation dialogs. Backup keys and passwords are no longer saved in the browser.
- `crypt decrypt` / `encrypt` create the output folder if it's missing.

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
