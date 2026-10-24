# finances

Private, **read-only** dashboard that unifies Santander, Revolut and Trade Republic.
Data is fetched from the providers, stored and analysed locally, and shown in a
mobile-first web app. Designed to run on an Android phone with Termux (see
[docs/TERMUX.md](docs/TERMUX.md)); it also runs on any machine with Python 3.11+.

## Privacy model (this repo is public)

- The repo contains **code only** plus fake demo data.
- Real data and secrets live in `$FINANCES_HOME` (default `~/.finances`), outside the checkout:
  - `finances.db.enc`: the SQLite database, encrypted at rest (AES-256-GCM, scrypt key).
  - `secrets.enc`: API credentials, same encryption. Added with `finances secret <key>`.
- `.gitignore`, a pre-commit hook and CI (gitleaks) block data and secret files from being committed.
- The server binds to `127.0.0.1` only; sync requires a custom header and Host is checked.
- Connectors implement a read-only interface: no payments, orders or writes anywhere.

## Quick start (demo data)

```sh
pip install -e .
finances --demo serve      # http://localhost:8765
python -m unittest discover -s tests
```

## Status

| Piece | State |
|---|---|
| Encrypted vault, DB, analysis, dashboard, tests | done |
| Demo connector | done |
| Trade Republic connector (`pytr`): cash and holdings, session kept encrypted | done, untested against a real account |
| Trade Republic transaction history | TODO |
| Santander / Revolut connector (Enable Banking) | TODO |
| 2FA code prompt in the UI (`NeedsInput`) | done |
| Categorisation rules, richer estimates | TODO |
