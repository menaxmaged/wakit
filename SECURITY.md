# Security

Please report vulnerabilities privately through [GitHub's advisory form](https://github.com/menaxmaged/wakit/security/advisories/new), not in a public issue.

How wakit handles your data:
- It never sends anything over the network. The only connections are to your own devices over adb and libimobiledevice, plus the GUI.
- The GUI listens only on `127.0.0.1` and requires the one-time token in the link `wakit gui` prints.
- Backup keys passed to `wakit crypt` are used in memory only and are never written anywhere. Keep in mind that a key typed on the command line can end up in your shell history.
