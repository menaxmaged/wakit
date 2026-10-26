# Contributing

Thanks for helping! A few rules keep wakit safe for everyone's chats:

- **No real data, ever.** Tests, fixtures, screenshots and issue reports use synthetic data only. Use made-up names like "Alex" and "Sam", and numbers like 15551234567. `.gitignore` blocks `*.db`, `*.sqlite`, `*.crypt*` and media folders. Don't work around it.
- **Dry run first.** Anything that writes has to default to a dry run, print what it would change, and make a backup before writing.
- **No dependencies in the core.** wakit runs on the standard library. `cryptography` is an optional extra used only by `wakit crypt`.
- **One module per command** in `src/wakit/`, each with a `build_parser()` (the GUI builds its forms from it) and `main(argv)`.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[crypt]"
python -m unittest discover -s tests -v
wakit gui
```

WhatsApp changes its database layout over time. If you find a new layout, describe the tables and columns in an issue or PR, and update [docs/how-it-works.md](docs/how-it-works.md). Never include rows from your own database.
