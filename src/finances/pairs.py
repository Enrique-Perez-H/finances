"""Spend + reimbursement pairs: equal and opposite entries cancel out.

When a payment and a later receipt of (nearly) the same amount exist, e.g. you pay a
restaurant bill and a friend sends you their share back, both are bypassed in spending,
income, recurring costs and forecasts. Nothing is deleted; every pair is listed so you can
reject a wrong one. Salary and interest are never treated as reimbursements.
"""
from bisect import bisect_left
from datetime import date, timedelta

from .categorize import INTERNAL
from .db import Database

TOLERANCE = 0.01          # amounts within 1% of the larger one count as equal
WINDOW_DAYS = 60          # the reimbursement arrives up to this long after the spend
EARLY_DAYS = 2            # ...or at most this many days before it
MIN_AMOUNT = 1.0
NOT_REIMBURSEMENTS = ("Salary", "Pension & benefits", "Interest & dividends")


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def find_pairs(rows: list[dict]) -> dict[str, str]:
    """rows: id, date, amount, category. Returns {id: partner id} for both sides."""
    spends = sorted((r for r in rows if r["amount"] < 0), key=lambda r: (r["date"], r["id"]))
    receipts = sorted((r for r in rows if r["amount"] > 0 and r["category"] not in NOT_REIMBURSEMENTS),
                      key=lambda r: (r["date"], r["id"]))
    used, out = set(), {}
    dates = [r["date"] for r in receipts]
    for spend in spends:
        best = None
        start = (date.fromisoformat(spend["date"]) - timedelta(days=EARLY_DAYS)).isoformat()
        for rec in receipts[bisect_left(dates, start):]:
            gap = _days(rec["date"], spend["date"])
            if gap > WINDOW_DAYS:
                break  # receipts are sorted by date: nothing later can match
            if rec["id"] in used:
                continue
            diff = abs(rec["amount"] + spend["amount"])
            if diff > TOLERANCE * max(rec["amount"], -spend["amount"]):
                continue
            key = (diff, abs(gap), rec["id"])
            if best is None or key < best[0]:
                best = (key, rec)
        if best:
            used.add(best[1]["id"])
            out[spend["id"]], out[best[1]["id"]] = best[1]["id"], spend["id"]
    return out


def apply(db: Database) -> int:
    """Recompute all pairs; returns how many entries changed state."""
    rows = [dict(r) for r in db.conn.execute(
        "SELECT id, date, amount, category FROM transactions"
        " WHERE category != ? AND pair_blocked = 0 AND ABS(amount) >= ?", (INTERNAL, MIN_AMOUNT))]
    wanted = find_pairs(rows)
    current = {r["id"]: r["paired_with"] for r in db.conn.execute(
        "SELECT id, paired_with FROM transactions WHERE paired_with != ''")}
    changed = 0
    for tx_id, partner in current.items():
        if wanted.get(tx_id, "") != partner:
            db.conn.execute("UPDATE transactions SET paired_with='' WHERE id=?", (tx_id,))
            changed += 1
    for tx_id, partner in wanted.items():
        if current.get(tx_id, "") != partner:
            db.conn.execute("UPDATE transactions SET paired_with=? WHERE id=?", (partner, tx_id))
            changed += 1
    return changed


def listing(db: Database) -> dict:
    rows = db.conn.execute(
        "SELECT s.id sid, s.date sdate, s.amount samount, s.description sdesc,"
        " r.id rid, r.date rdate, r.amount ramount, r.description rdesc"
        " FROM transactions s JOIN transactions r ON r.id = s.paired_with"
        " WHERE s.amount < 0 ORDER BY s.date DESC").fetchall()
    pairs = [dict(spend=dict(id=r["sid"], date=r["sdate"], amount=r["samount"], description=r["sdesc"]),
                  reimbursement=dict(id=r["rid"], date=r["rdate"], amount=r["ramount"],
                                     description=r["rdesc"])) for r in rows]
    return dict(pairs=pairs, count=len(pairs),
                spent=round(sum(-p["spend"]["amount"] for p in pairs), 2),
                reimbursed=round(sum(p["reimbursement"]["amount"] for p in pairs), 2))


def block(db: Database, tx_id: str) -> None:
    """You say this entry and its partner are unrelated."""
    row = db.conn.execute("SELECT paired_with FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise ValueError("unknown transaction")
    ids = [tx_id] + ([row["paired_with"]] if row["paired_with"] else [])
    for i in ids:
        db.conn.execute("UPDATE transactions SET pair_blocked=1, paired_with='' WHERE id=?", (i,))
    apply(db)
