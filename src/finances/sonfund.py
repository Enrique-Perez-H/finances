"""A child's share of the interest paid on one account (e.g. Trade Republic cash).

The money is not separated at the bank. You say how much of the account is the child's
at a start date (and any later deposits you set aside for them). Every monthly interest
payment, already net of tax, is split in proportion to the child's share of the account
balance before the payment; the child's part stays in the account and earns interest too.

Share for month m = child's amount at the end of m-1 / account balance at the end of m-1.
Deposits made during month m count from m on. The projection continues with a rate you set
or one estimated from the real payments. An estimate, not advice.
"""
import calendar
from datetime import date

from .categorize import normalize
from .db import Database

INTEREST = "Interest & dividends"
MARKER = "tr interest"  # Trade Republic marks interest payouts, so dividends are not shared


def _iso(value, name):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD") from None


def _num(body, key, lo, hi, default=None):
    raw = body.get(key, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return v


def get_config(db: Database) -> dict | None:
    row = db.conn.execute("SELECT * FROM son_fund WHERE id=1").fetchone()
    return dict(row) if row else None


def save_config(db: Database, body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("name must be 1 to 60 characters")
    account = str(body.get("account_id", ""))
    if not db.conn.execute("SELECT 1 FROM accounts WHERE id=?", (account,)).fetchone():
        raise ValueError("choose the account the interest is paid into")
    rate = body.get("annual_rate")
    cfg = dict(name=name, account_id=account,
               start_date=_iso(body.get("start_date") or date.today().isoformat(), "start_date"),
               initial=_num(body, "initial", 0, 1e9, 0), monthly=_num(body, "monthly", 0, 1e7, 0),
               annual_rate=None if rate in (None, "") else _num(body, "annual_rate", -50, 100),
               years=int(_num(body, "years", 1, 40, 18)))
    db.conn.execute(
        "INSERT INTO son_fund(id, name, account_id, start_date, initial, monthly, annual_rate, years)"
        " VALUES(1, :name, :account_id, :start_date, :initial, :monthly, :annual_rate, :years)"
        " ON CONFLICT(id) DO UPDATE SET name=:name, account_id=:account_id, start_date=:start_date,"
        " initial=:initial, monthly=:monthly, annual_rate=:annual_rate, years=:years", cfg)
    return cfg


def add_deposit(db: Database, body: dict) -> dict:
    dep = dict(date=_iso(body.get("date"), "date"), amount=_num(body, "amount", 0.01, 1e9),
               note=str(body.get("note", ""))[:80])
    dep["id"] = db.conn.execute("INSERT INTO son_deposits(date, amount, note) VALUES(:date,:amount,:note)",
                                dep).lastrowid
    return dep


def delete_deposit(db: Database, dep_id) -> None:
    db.conn.execute("DELETE FROM son_deposits WHERE id=?", (int(dep_id),))


def _month_end(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1]).isoformat()


def _prev(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - int(m == 1)}-{(m - 2) % 12 + 1:02d}"


def _next(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + int(m == 12)}-{(m % 12) + 1:02d}"


def simulate(db: Database, today: date | None = None) -> dict:
    cfg = get_config(db)
    if not cfg:
        return dict(configured=False)
    today = today or date.today()
    current = today.isoformat()[:7]
    notes = []
    acct = db.conn.execute("SELECT balance FROM accounts WHERE id=?", (cfg["account_id"],)).fetchone()
    if not acct:
        return dict(configured=True, config=cfg, rows=[], projection=[], notes=["The chosen account no longer exists."])
    txs = [(r["date"], r["amount"]) for r in db.conn.execute(
        "SELECT date, amount FROM transactions WHERE account_id=? ORDER BY date", (cfg["account_id"],))]

    def balance_at(day):  # rebuilt backwards from today's balance, known from the first transaction on
        if not txs or day < txs[0][0]:
            return None
        return acct["balance"] - sum(a for d, a in txs if d > day)

    paid = [(r["date"][:7], r["amount"], r["description"]) for r in db.conn.execute(
        "SELECT date, amount, description FROM transactions WHERE account_id=? AND category=? AND amount>0"
        " AND paired_with=''", (cfg["account_id"], INTEREST))]
    marked = [p for p in paid if MARKER in normalize(p[2])]
    interest = {}
    for month, amount, _ in (marked or paid):  # only real interest when it can be told apart
        interest[month] = interest.get(month, 0.0) + amount
    if not interest:
        notes.append("No interest payments found in this account yet. Sync Trade Republic to import them.")

    deposits = {}
    for r in db.conn.execute("SELECT date, amount FROM son_deposits WHERE date >= ? ORDER BY date",
                             (cfg["start_date"],)):
        deposits[r["date"][:7]] = deposits.get(r["date"][:7], 0.0) + r["amount"]

    start_month = cfg["start_date"][:7]
    son = cfg["initial"] + deposits.get(start_month, 0.0)
    rows, month, ratios = [], _next(start_month), []
    while month <= current:
        prev_balance = balance_at(_month_end(_prev(month)))
        paid_now = interest.get(month, 0.0)
        share = min(1.0, son / prev_balance) if prev_balance and prev_balance > 0 else None
        son_interest = paid_now * share if share is not None else 0.0
        if share is None and paid_now:
            notes.append(f"{month}: account balance unknown, that payment was not shared.")
        if share is not None and paid_now > 0 and month < current:
            ratios.append(paid_now / prev_balance)
        added = deposits.get(month, 0.0)
        son += son_interest + added
        rows.append(dict(month=month, account_balance=None if prev_balance is None else round(prev_balance, 2),
                         interest=round(paid_now, 2), share=None if share is None else round(share, 4),
                         son_interest=round(son_interest, 2), deposits=round(added, 2),
                         son_balance=round(son, 2), partial=month == current))
        month = _next(month)

    estimated = None
    if ratios:
        monthly = sum(ratios[-12:]) / len(ratios[-12:])
        estimated = round(((1 + monthly) ** 12 - 1) * 100, 2)
    rate = cfg["annual_rate"] if cfg["annual_rate"] is not None else estimated
    projection = []
    if rate is None:
        notes.append("No rate to project with: set one, or wait for interest payments to estimate it.")
    else:
        r = (1 + rate / 100) ** (1 / 12) - 1
        value, month = son, _next(current)
        for _ in range(cfg["years"] * 12):
            gained = value * r
            value += gained + cfg["monthly"]
            projection.append(dict(month=month, son_balance=round(value, 2), son_interest=round(gained, 2)))
            month = _next(month)
    now_balance = acct["balance"]
    last12 = [r for r in rows if not r["partial"]][-12:]
    return dict(
        configured=True, config=cfg, rows=rows, projection=projection, notes=notes,
        son_balance=round(son, 2), account_balance=round(now_balance, 2),
        share_now=round(son / now_balance, 4) if now_balance > 0 else None,
        interest_last_12=round(sum(r["son_interest"] for r in last12), 2),
        interest_total=round(sum(r["son_interest"] for r in rows), 2),
        deposits_total=round(sum(deposits.values()), 2),
        rate_used=rate, rate_estimated=estimated,
        projected_end=projection[-1]["son_balance"] if projection else None)


def payload(db: Database, today: date | None = None) -> dict:
    out = simulate(db, today)
    out["deposits"] = [dict(r) for r in db.conn.execute(
        "SELECT id, date, amount, note FROM son_deposits ORDER BY date")]
    out["accounts"] = [dict(r) for r in db.conn.execute("SELECT id, name, balance FROM accounts ORDER BY name")]
    return out
