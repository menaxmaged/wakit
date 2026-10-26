# wakit

[![PyPI](https://img.shields.io/pypi/v/wakit?color=128c5e)](https://pypi.org/project/wakit/)
[![tests](https://github.com/menaxmaged/wakit/actions/workflows/test.yml/badge.svg)](https://github.com/menaxmaged/wakit/actions/workflows/test.yml)
[![Python](https://img.shields.io/pypi/pyversions/wakit)](https://pypi.org/project/wakit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/menaxmaged/wakit/blob/main/LICENSE)

**Move your WhatsApp chat history between chat exports, Android and iPhone. Free, local, open source.**

![wakit turning a chat export into history: dry run, apply, and a second run that finds nothing new](https://raw.githubusercontent.com/menaxmaged/wakit/main/docs/assets/demo.gif)

wakit is a command-line tool with a local web GUI. It can:

- turn **"Export chat"** files (`.zip` / `_chat.txt` with media) back into real chat history on Android
- merge an **old Android database** (old phone, WhatsApp mod, adb backup) into the current one
- bring an **iPhone's** chats to **Android**, and an **Android** phone's chats to an **iPhone**, with no paid tool and no factory reset of the Android side
- copy your **favourite stickers** across
- handle the fiddly device parts: pulling and installing databases on a rooted phone, and checking, repairing and restoring iPhone backups

Everything runs on your own computer. Nothing is uploaded anywhere.

**Website:** https://wakit-site.vercel.app

> wakit is not affiliated with WhatsApp or Meta. It edits your own WhatsApp databases, so **keep backups**.
> Every tool is a dry run until you pass `--apply`, and each write makes a backup first.

---

## Download

You don't need Python. Download the app for your computer from the [latest release](https://github.com/menaxmaged/wakit/releases/latest), then double-click it to open the GUI, or run it in a terminal with a command (`wakit import …`).

| System | File |
|---|---|
| macOS (Apple silicon) | [`wakit-macos-arm64`](https://github.com/menaxmaged/wakit/releases/latest/download/wakit-macos-arm64) |
| Windows | [`wakit-windows-x86_64.exe`](https://github.com/menaxmaged/wakit/releases/latest/download/wakit-windows-x86_64.exe) |
| Linux (x86_64) | [`wakit-linux-x86_64`](https://github.com/menaxmaged/wakit/releases/latest/download/wakit-linux-x86_64) |

The apps aren't signed, so the first launch needs one extra step:
- **macOS:** run `chmod +x wakit-macos-arm64 && xattr -d com.apple.quarantine wakit-macos-arm64` once, or right-click it and choose **Open**.
- **Windows:** choose **More info → Run anyway**.
- **Linux:** run `chmod +x wakit-linux-x86_64`.

Intel Macs and other systems: install with pip as shown below.

## Install with pip

Needs Python 3.9+. There are no Python dependencies.

```sh
pipx install wakit          # or: pip install wakit
wakit                       # list the commands
wakit gui                   # open the GUI in your browser
```

For Android's encrypted backups (`wakit crypt`), add the one optional dependency: `pipx install "wakit[crypt]"`.

Device tools, only for the steps that touch a phone:

| For | macOS | Linux |
|---|---|---|
| Android (`device`, `--push`) | `brew install android-platform-tools` | `apt install adb` |
| iPhone (`ios-backup backup/restore`) | `brew install libimobiledevice` | `apt install libimobiledevice-utils` |

Most Android steps work on a **rooted** phone, because WhatsApp's database lives in `/data/data/com.whatsapp`, where only root can read and write. **No root?** Use the phone's encrypted backup instead. See [workflow 5](https://github.com/menaxmaged/wakit#5-no-root-edit-androids-encrypted-backup-experimental). That needs one extra package: `pip install "wakit[crypt]"`. The iPhone side needs **no jailbreak**. It works through an ordinary unencrypted backup.

## The GUI

```sh
wakit gui
```

This opens a page served by your computer only (`127.0.0.1`, with a one-time access token in the link). It has:

- **Guides**: the workflows below, step by step. Each step opens the matching tool.
- **A form for every command**, with file pickers, so you never type a path.
- **Dry run / Apply** buttons and a live log of each run.

![The wakit GUI: step-by-step guides for each workflow](https://raw.githubusercontent.com/menaxmaged/wakit/main/docs/assets/gui.jpg)

## Workflows

### 1. Add an exported chat to Android

```sh
wakit device pull --out phone/                    # WhatsApp is disabled while copying
wakit import --db phone/msgstore.db --export "WhatsApp Chat - Alex.zip" --contact 15551234567
#   ... check the counts, then:
wakit import --db phone/msgstore.db --export "WhatsApp Chat - Alex.zip" --contact 15551234567 \
             --apply --push --phone-root /sdcard/WhatsApp
wakit device install --db phone/msgstore.db       # owner + SELinux label copied, then WhatsApp starts
```

- Reads iPhone exports (`[dd/mm/yyyy, h:mm:ss AM] Name: …`) and Android exports (`dd/mm/yyyy, hh:mm - Name: …`) in either date order.
- Brings back text, photos, videos, voice notes, stickers, documents, contact cards, captions and deleted-message markers.
- Messages already in the chat are skipped, so re-running never duplicates anything.
- **Exported from the other person's phone?** Then the "You …" lines are theirs. Pass `--me "Your Name"` with your name as it appears in the export.

### 2. iPhone → Android

```sh
wakit ios-backup backup --out ~/Backups           # or use a Finder / 3uTools backup (unencrypted)
wakit device pull --out phone/
wakit from-ios --db phone/msgstore.db --backup ~/Backups/<device-id>          # dry run
wakit from-ios --db phone/msgstore.db --backup ~/Backups/<device-id> --apply --push --phone-root /sdcard/WhatsApp
wakit stickers-from-ios --db phone/stickers.db --backup ~/Backups/<device-id> --files-out Stickers --apply
wakit device install --db phone/msgstore.db
```

Chats are matched by WhatsApp id (including the newer `@lid` ids), so the iPhone history and the Android history of the same person end up in one chat.

### 3. Android → iPhone

```sh
wakit device pull --out phone/
wakit device pull-media --out phone-media/        # one tar stream, much faster than adb pull
wakit ios-backup backup --out ~/Backups           # a FRESH, FULL, unencrypted backup
cp -cR ~/Backups/<device-id> ~/Backups/edit/      # work on a copy (cp -c clones on APFS, using no extra space)
wakit to-ios --db phone/msgstore.db --backup ~/Backups/edit/<device-id> --media phone-media/          # dry run
wakit to-ios --db phone/msgstore.db --backup ~/Backups/edit/<device-id> --media phone-media/ --apply
wakit ios-backup restore --backup ~/Backups/edit/<device-id> --yes
```

`to-ios` keeps the iPhone's own WhatsApp history and adds what it is missing: chats, groups, photos, voice notes, stickers, documents, replies and stars. It also makes thumbnails (on macOS) and repairs the backup's file records.

> ⚠️ **A restore replaces everything on the iPhone with the backup's contents.**
> - Use a backup made **just before**. Anything added to the iPhone after the backup is lost.
> - **Never** restore a backup cut down to WhatsApp only. The phone comes back with nothing but WhatsApp. `ios-backup restore` refuses such backups.
> - Turn **Find My** off, and keep the iPhone **unlocked** (Auto-Lock: Never) until it reboots.
> - When WhatsApp opens, verify your number and **skip** any iCloud restore it offers. The Android phone gets logged out.

### 4. Merge two Android databases

```sh
wakit merge --old old/msgstore.db --new phone/msgstore.db --fuzzy            # dry run
wakit merge --old old/msgstore.db --new phone/msgstore.db --fuzzy --apply
wakit stickers --db phone/stickers.db --from old/stickers.db --from-files old/files/Stickers --files-out Stickers --apply
```

### 5. No root: edit Android's encrypted backup (experimental)

WhatsApp's own local backup (`msgstore.db.crypt15`) can be decrypted and re-encrypted with your backup key. Every tool above can then work on it with no root.

1. Make the key. In WhatsApp, go to **Settings → Chats → Chat backup → End-to-end encrypted backup**, choose **Use 64-digit encryption key instead**, write the key down, and back up.
2. Copy the backup off the phone. It's in `Android/media/com.whatsapp/WhatsApp/Databases/` (on older phones, `WhatsApp/Databases/`). A file manager or plain `adb pull` can read it without root.

```sh
wakit crypt decrypt --in msgstore.db.crypt15 --key "<your 64-digit key>" --out msgstore.db
wakit import --db msgstore.db --export "WhatsApp Chat - Alex.zip" --contact 15551234567 --apply   # or from-ios, merge...
wakit crypt encrypt --in msgstore.db --like msgstore.db.crypt15 --key "<your 64-digit key>" --out new/msgstore.db.crypt15
```

3. Put the file back. Copy `new/msgstore.db.crypt15` into the same `Databases/` folder on the phone, replacing the old file (keep a copy of it). Copy any media into `WhatsApp/Media/`.
4. Reinstall WhatsApp, or clear its data. Verify your number and tap **Restore** when it finds the local backup.

`crypt` also opens `.crypt14` backups using the 158-byte `key` file from `/data/data/com.whatsapp/files/key`. Copying that file needs root once. Decrypting and re-encrypting is verified on real crypt14 backups, and `encrypt` checks its own output by decrypting it again. WhatsApp actually restoring an edited backup hasn't been tested on a phone yet, so keep the original file.

## Commands

| Command | What it does |
|---|---|
| `import` | "Export chat" .zip/.txt → Android `msgstore.db` |
| `crypt info \| decrypt \| encrypt` | open and re-seal `.crypt14`/`.crypt15` backups (no root) |
| `merge` | one Android `msgstore.db` → another (messages, media, replies, reactions, calls) |
| `stickers` | favourite/recent stickers between Android `stickers.db` files |
| `from-ios` | iPhone backup → Android `msgstore.db` |
| `stickers-from-ios` | iPhone stickers → Android `stickers.db` |
| `to-ios` | Android `msgstore.db` + media → iPhone backup |
| `ios-backup info \| extract \| check [--fix] \| backup \| restore` | iPhone backup tools |
| `device info \| pull \| install \| enable \| pull-media \| push-media` | rooted Android over adb |
| `gui` | the local web GUI |

Run `wakit <command> -h` for every option. Use `wakit --serial <device>` to pick an adb device, for example Wi-Fi adb at `192.168.1.20:5555`.

## Good to know

- **"Something went wrong with chat history"** after installing a database means the file has the wrong owner or SELinux label. `wakit device install` copies both from the original file.
- **"Restore chat history" prompt**: WhatsApp opened a half-copied file. It restarts itself seconds after a force-stop, which is why `device pull/install` disable the app during the copy.
- **iPhone restore fails with error 205**: the backup has records whose size or checksum doesn't match the file. This is common in backups from third-party tools. Run `wakit ios-backup check --fix`.
- **Encrypted iPhone backups** aren't supported. Turn encryption off before backing up. Android `.crypt14`/`.crypt15` backups are supported through `wakit crypt`.
- **Not carried over:**
  - Android → iPhone: reactions, polls and system notices.
  - iPhone → Android: polls, reactions and calls.
- **Tested with** WhatsApp 2.26 on a rooted Android 7.1 tablet and on iOS 27. Other versions should work but haven't been tried. WhatsApp changes its database layout from time to time. If something looks off, open an issue with the dry-run output, but **never share your databases**.

How it works inside (schemas, sort order, the protobuf blobs, the backup format): [docs/how-it-works.md](https://github.com/menaxmaged/wakit/blob/main/docs/how-it-works.md).

## Development

```sh
git clone https://github.com/menaxmaged/wakit && cd wakit
python -m venv .venv && . .venv/bin/activate
pip install -e ".[crypt]"                  # editable: changes in src/ take effect right away
python -m unittest discover -s tests -v
wakit gui
```

The code is in `src/wakit/`: one module per command (`cli.py` dispatches to them), and `web/index.html` is the GUI page. The standalone apps are built with PyInstaller from `packaging/wakit.spec` (`pip install pyinstaller && pyinstaller packaging/wakit.spec`). See [CONTRIBUTING.md](https://github.com/menaxmaged/wakit/blob/main/CONTRIBUTING.md).

Releases: bump `version` in `pyproject.toml`, add a line to `CHANGELOG.md`, then `git tag -a vX.Y.Z -m "wakit X.Y.Z" && git push origin vX.Y.Z`. GitHub Actions tests it, publishes it to PyPI, and attaches the apps to the release.

The tests use synthetic data only. Never commit real databases or exports: `.gitignore` blocks `*.db`, `*.sqlite` and media folders.

## License

MIT. See [LICENSE](https://github.com/menaxmaged/wakit/blob/main/LICENSE).
