"""Balance evolution by account type, and interest received.

Cash and savings accounts are rebuilt backwards from today's balance using their
transactions, so their past is known as far as the transactions go. Investment accounts
have no such trail, so their history starts when you first synced (one snapshot per
sync). Earlier months for them are shown as unknown rather than guessed.
"""
import calendar
from datetime import date

from .categorize import INTERNAL
from .db import Database

KINDS = ("cash", "savings", "investment")
INTEREST = "Interest & dividends"


def snapshot(db: Database, today: date | None = None) -> None:
    day = (today or date.today()).isoformat()
    db.conn.execute("DELETE FROM balance_history WHERE day=?", (day,))
    db.conn.execute("INSERT INTO balance_history(day, account_id, balance)"
                    " SELECT ?, id, balance FROM accounts", (day,))


def set_kind(db: Database, account_id: str, kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not db.conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise ValueError("unknown account")
    db.conn.execute("INSERT INTO account_settings(id, kind) VALUES(?,?)"
                    " ON CONFLICT(id) DO UPDATE SET kind=excluded.kind", (account_id, kind))
    db.conn.execute("UPDATE accounts SET kind=? WHERE id=?", (kind, account_id))


def _month_ends(first: str, today: date) -> list[tuple[str, str]]:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while (y, m) <= (today.year, today.month):
        last = date(y, m, calendar.monthrange(y, m)[1])
        out.append((f"{y}-{m:02d}", min(last, today).isoformat()))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def evolution(db: Database, months: int = 24, today: date | None = None) -> dict:
    today = today or date.today()
    accounts = [dict(r) for r in db.conn.execute("SELECT id, name, kind, balance FROM accounts")]
    txs = {a["id"]: [] for a in accounts}
    for r in db.conn.execute("SELECT account_id, date, amount FROM transactions ORDER BY date"):
        txs.setdefault(r["account_id"], []).append((r["date"], r["amount"]))
    snaps = {a["id"]: [] for a in accounts}
    for r in db.conn.execute("SELECT account_id, day, balance FROM balance_history ORDER BY day"):
        snaps.setdefault(r["account_id"], []).append((r["day"], r["balance"]))

    def at(acct, day):
        if day >= today.isoformat():
            return acct["balance"]
        rows = txs[acct["id"]]
        # An account that has transactions is rebuilt from them whatever type you gave it
        # (e.g. Trade Republic cash counted as an investment). Portfolios have none.
        if rows and day >= rows[0][0]:
            return acct["balance"] - sum(a for d, a in rows if d > day)
        known = [b for d, b in snaps[acct["id"]] if d <= day]
        return known[-1] if known else None

    candidates = [d for rows in txs.values() for d, _ in rows[:1]] + \
                 [d for rows in snaps.values() for d, _ in rows[:1]]
    if not accounts or not candidates:
        return dict(rows=[], latest=None, changes={}, accounts=accounts, interest=interest(db, today))
    rows = []
    for month, day in _month_ends(min(candidates)[:7], today):
        per_kind, partial = {}, False
        for acct in accounts:
            bal = at(acct, day)
            if bal is None:
                partial = True
                continue
            per_kind[acct["kind"]] = per_kind.get(acct["kind"], 0.0) + bal
        rows.append(dict(month=month, partial=partial,
                         **{k: (round(per_kind[k], 2) if k in per_kind else None) for k in KINDS},
                         total=round(sum(per_kind.values()), 2) if per_kind else None))
    rows = rows[-months:]

    def change(key, back):
        if len(rows) <= back or rows[-1][key] is None or rows[-1 - back][key] is None:
            return None
        if key == "total" and (rows[-1]["partial"] or rows[-1 - back]["partial"]):
            return None  # a total missing one of its parts says nothing about growth
        return round(rows[-1][key] - rows[-1 - back][key], 2)

    changes = {k: {"month": change(k, 1), "year": change(k, 12)} for k in (*KINDS, "total")}
    return dict(rows=rows, latest=rows[-1], changes=changes, accounts=accounts,
                interest=interest(db, today))


def interest(db: Database, today: date | None = None) -> dict:
    """Interest and dividends received per month, and per account."""
    today = today or date.today()
    rows = db.conn.execute(
        "SELECT t.date, t.amount, a.name FROM transactions t JOIN accounts a ON a.id=t.account_id"
        " WHERE t.amount > 0 AND t.category=? AND t.category != ?", (INTEREST, INTERNAL)).fetchall()
    monthly, by_account = {}, {}
    for r in rows:
        monthly[r["date"][:7]] = monthly.get(r["date"][:7], 0.0) + r["amount"]
        by_account[r["name"]] = by_account.get(r["name"], 0.0) + r["amount"]
    labels, y, m = [], today.year, today.month
    for _ in range(12):
        labels.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    last12 = {m: v for m, v in monthly.items() if m in labels}
    return dict(
        monthly=[dict(month=m, amount=round(v, 2)) for m, v in sorted(monthly.items())][-24:],
        total=round(sum(monthly.values()), 2), last_12_months=round(sum(last12.values()), 2),
        average_per_month=round(sum(last12.values()) / 12, 2),
        by_account=[dict(account=k, total=round(v, 2)) for k, v in
                    sorted(by_account.items(), key=lambda kv: -kv[1])])
