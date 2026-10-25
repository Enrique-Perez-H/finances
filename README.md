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

## Tabs

| Tab | What it does |
|---|---|
| Overview | Your total and its evolution per type, plus income versus spending per month (chart and table with averages), with range buttons (6m, 12m, 2y, since start); tap or drag a chart to read a month |
| Spending | Spending per category per month, plus an average month (3m, 6m, 12m or all). Entries it cannot classify are put to you in a review queue; each answer becomes a rule applied to past and future entries. You can also fix single entries. A browser lists every entry by category and period (search, sort, move one, several or a whole shop). |
| Accounts | How each account counts (cash, savings, investment), interest and dividends received per month, month-by-month table |
| Income | What you receive: salary, transfers, interest, by type, source and month |
| Recurring | Detected recurring costs and income, monthly and yearly totals, what is coming up |
| Forecast | Projects income, spending and wealth from your history plus assumptions you set |
| Fund | Calculator for a savings fund (e.g. for a child): contributions, interest, tax, target, optionally starting from a real account balance |

Everything is computed locally. Internal transfers between your own accounts are excluded from spending and income, and so are reimbursement pairs: a payment and a receipt of the same amount (within 1%, up to 60 days apart) cancel out. Every pair is listed in the Spending tab, with a button to reject a wrong match.

## Status

| Piece | State |
|---|---|
| Encrypted vault, DB, analysis, dashboard, tests | done |
| Demo connector | done |
| Trade Republic connector (`pytr`): cash and holdings, session kept encrypted | done, untested against a real account |
| Trade Republic transaction history | TODO |
| Santander connector (Enable Banking, balances + transactions) | done, untested against the real API |
| Revolut | not planned for now |
| 2FA code prompt in the UI (`NeedsInput`) | done |
| Spending categories, review queue, income, recurring, forecast, fund tabs | done (v0.2 to v0.5) |
| Trade Republic history import (interest, deposits, card payments) | done, untested against a real account |
| Compare fund plan against real contributions | idea |
