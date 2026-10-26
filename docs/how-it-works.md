# How wakit works

These notes cover what wakit relies on in WhatsApp's databases and in iPhone backups. They were worked out
by reading real databases (WhatsApp 2.26, October 2026). WhatsApp changes these layouts without notice, so treat
them as observations, not a specification.

## Android: `msgstore.db`

| Table | Used for |
|---|---|
| `message` | one row per message: `chat_row_id`, `from_me`, `key_id`, `sender_jid_row_id`, `timestamp` (ms), `message_type`, `text_data` (also captions), `sort_id` |
| `chat` | one row per chat; `jid_row_id`, pointers to the display/last/last-read message (`*_row_id` and `*_sort_id`) |
| `jid`, `jid_map` | every WhatsApp id (`raw_string`), and the map between phone-number ids (`@s.whatsapp.net`) and the newer `@lid` ids |
| `message_media` | file path (relative to the WhatsApp folder, e.g. `Media/WhatsApp Images/…`), size, mime, width/height, duration, sha256 |
| `message_quoted`, `message_add_on` | replies and reactions |
| `message_ftsv2` | search index: `docid` = `message._id`, `fts_jid` = `"<sender> <chat>"` with each id written as base36(jid row id + 10); `1` is you |

Message types used: 0 text, 1 image, 2 audio, 3 video, 4 contact, 5 location, 9 document, 13 GIF, 15 deleted, 20 sticker.

**Sort order.** The app gives each new message `sort_id = _id`, and it orders a chat by `sort_id`. If imported
rows get sort ids above the largest `_id`, every new message sorts *before* them, into the middle of the chat. So after each
import wakit renumbers `sort_id` across the whole database in time order (`0 … n-1`, which keeps every
`sort_id ≤ max(_id)`) and recomputes each chat's `*_sort_id` pointers from its `*_row_id` columns.

**New chats** need the state columns the app expects (`chat_encryption_state = 2`, `plaintext_disabled = 1`, …).
wakit copies them from a chat the app created itself. `account_jid_row_id` is unique and equals `jid_row_id`.
Since the move to `@lid`, one-to-one chats are keyed by the `@lid` jid (jid `type` 18).

**Installing on the phone.** The database must keep the app's owner (`u0_aNNN`) and its SELinux label
(`u:object_r:app_data_file:s0:cNNN,cNNN`), or WhatsApp reports *"something went wrong with chat history"*.
WhatsApp restarts seconds after a force-stop, and if it opens a half-copied file it offers to restore from
backup. So `device install` does this:

1. `pm disable` the app.
2. Push the new database next to the old one as `.tmp`, set the owner, mode and label, and `mv` it into place.
3. `pm enable` the app, wake the screen and launch it.

## Chat exports

- **iPhone:** `[dd/mm/yyyy, h:mm:ss AM] Name: text`, with `‎<attached: file>` for media. Lines prefixed with U+200E (LRM) are
  system notices or media.
- **Android:** `dd/mm/yyyy, hh:mm - Name: text`, with `file (file attached)` or `<Media omitted>`. Times are only to the minute.

Date order (dmy or mdy) is detected from values above 12. Duplicates are matched one-to-one with a time window:
the same sender, the same text (2 min, or 15 min for texts of 6+ characters, since iPhone backups date messages by when they
were *received*) or the same media type (about 1 min).

## iPhone: `ChatStorage.sqlite` (Core Data)

| Table | Used for |
|---|---|
| `ZWACHATSESSION` | chats: `ZCONTACTJID` (`@lid` / `@g.us`), `ZCONTACTIDENTIFIER` (phone-number jid), `ZPARTNERNAME`, `ZMESSAGECOUNTER`, `ZLASTMESSAGE` |
| `ZWAMESSAGE` | messages: `ZMESSAGEDATE` (seconds since 2001-01-01), `ZISFROMME`, `ZMESSAGETYPE`, `ZTEXT`, `ZSTANZAID` (= Android `key_id`), `ZSORT`, `ZGROUPMEMBER`, `ZMEDIAITEM` |
| `ZWAMEDIAITEM` | every message has one. For media: `ZMEDIALOCALPATH` (`Media/<chat jid>/<x>/<y>/<uuid>.<ext>`), `ZXMPPTHUMBPATH`, size, duration, and height/width in `ZLATITUDE`/`ZLONGITUDE` |
| `ZWAGROUPMEMBER`, `ZWAGROUPINFO` | group senders and group rows |
| `ZWAMESSAGE_fts` | FTS3 search: `_id` = message `Z_PK`, `content` = text |
| `Z_PRIMARYKEY` | Core Data's next id per entity (`Z_MAX`), which must be raised after inserting |

iPhone types: 0 text, 1 image, 2 video, 3 audio, 4 contact, 5 location, 8 document, 11 GIF, 14 deleted, 15 sticker.

- **`ZSORT`** is a per-chat sequence (1…n), and `ZMESSAGECOUNTER` is n + 1.
- **`ZLASTSESSION`** is set only on a chat's last message.
- **`ZMEDIASECTIONID`** (`yyyy-mm`) puts photos, videos and documents in the media gallery.
- **Two protobuf blobs** sit next to each message:
  - `ZWAMESSAGE.ZPUSHNAME` (base64) holds send-state fields. Field 3 of a sent message is your own `@lid` id.
  - `ZWAMEDIAITEM.ZMETADATA` holds reply context: field 5 is the quoted message id, 6 its sender, and 19 the quoted message, with `{1: text}` for text.

  wakit writes the same fields for the messages it adds.

## iPhone backups

An unencrypted backup is a folder named after the device id, containing:

- `Manifest.db`: the `Files` table, with one row per file: `fileID`, `domain`, `relativePath`, `flags` (1 file, 2 folder) and an `NSKeyedArchiver` `MBFile` record holding Size, Mode, times and inode.
- `Manifest.plist` and `Info.plist`.
- The files themselves, at `<fileID[:2]>/<fileID>`, where `fileID = sha1("<domain>-<relativePath>")`.

WhatsApp's data lives in the domain `AppDomainGroup-group.net.whatsapp.WhatsApp.shared`, which holds `ChatStorage.sqlite`, `Message/Media/…` and `Sticker.sqlite`.

**Restores** (`idevicebackup2 restore --system --settings`):

- A restore replaces the device's data with what the backup contains. Even without `--remove`, restoring a backup
  that holds only WhatsApp left the phone with only WhatsApp's data. Restore full backups only.
- iOS rejects the whole restore (MBErrorDomain 205) if any record's `Size` or `Digest` doesn't match its file, or a
  listed file is missing. Third-party backup tools produce such records when apps write during the backup.
  `ios-backup check --fix` corrects the records and drops the missing files.
- Some tools write `Info.plist` without the `<?xml?>` line, which `idevicebackup2` can't read. `--fix` adds it.
- Find My must be off, and the iPhone must stay unlocked until it reboots.

## Android encrypted backups (`.crypt14` / `.crypt15`)

A local backup file is laid out like this:

```
[header length: 1 byte][0x01, if a feature table follows][protobuf header]
[AES-256-GCM ciphertext][16-byte GCM tag][16-byte MD5 of everything before]
```

- **Header** (protobuf):
  - **crypt15:** field 3 = `{1: 16-byte IV}`.
  - **crypt14:** field 2 = `{…, 3: server salt, 4: id salt, 5: 16-byte IV}`.
  - **Both:** field 4 = app info (`{1: "2.26.41.77", …}`).

  The encrypted data starts right after the header. Read the offset from the header rather than hard-coding it: it changes between versions.
- **Contents:** usually `zlib(database)`. Some backups, such as `stickers.db.crypt14`, hold a ZIP with the database inside and end in just the GCM tag, with no MD5. `wakit crypt` detects both and writes re-encrypted files packed the same way as the original.
- **Keys:**
  - **crypt14:** the AES key is the last 32 bytes of the 158-byte Java-serialized `files/key`.
  - **crypt15:** the 64-digit key of an end-to-end encrypted backup is a 32-byte root key. The AES key is `HKDF-SHA256(root, salt = 32 zero bytes, info = "backup encryption")`, the first 32 bytes.
- **No search index:** the database inside a backup has no `message_ftsv2` table. WhatsApp rebuilds it after a restore, so wakit leaves it out when writing into a decrypted backup.
- **Re-encrypting:** wakit keeps the original header and swaps in a fresh random IV, because an IV must never be reused with the same key. It then encrypts and adds the tag, plus the MD5 if the original had one.

The layout was first worked out against the open-source
[whatsapp-msgstore-web-viewer](https://github.com/trevordixon/whatsapp-msgstore-web-viewer), then checked on real
crypt14 backups (decrypted byte for byte, and round-tripped). wakit's code is its own.
