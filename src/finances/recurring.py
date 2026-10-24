"""Detect recurring payments and income from transaction history.

Entries are grouped by merchant, split by direction. A group is recurring when it
has at least 3 occurrences whose gaps mostly match one cadence (weekly, monthly,
quarterly, ...). Amounts that vary a lot (utility bills) are flagged `variable`.
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .categorize import INTERNAL, merchant_key, normalize
from .db import Database

# (name, min days, max days, typical days, payments per month)
CADENCES = [
    ("weekly", 5, 9, 7, 52 / 12),
    ("biweekly", 12, 17, 14, 26 / 12),
    ("monthly", 26, 35, 30.44, 1.0),
    ("quarterly", 80, 100, 91, 1 / 3),
    ("semiannual", 170, 195, 182, 1 / 6),
    ("yearly", 350, 380, 365, 1 / 12),
]
MIN_OCCURRENCES = 3
MIN_GAP_MATCH = 0.7


def _cadence(gaps: list[int]):
    med = median(gaps)
    for name, lo, hi, typical, per_month in CADENCES:
        if lo <= med <= hi:
            ok = sum(lo <= g <= hi for g in gaps) / len(gaps)
            if ok >= MIN_GAP_MATCH:
                return name, typical, per_month
    return None


def detect(db: Database, today: date | None = None) -> list[dict]:
    today = today or date.today()
    groups = defaultdict(list)
    for r in db.conn.execute(
            "SELECT date, amount, description, category FROM transactions"
            " WHERE category != ? AND amount != 0 AND paired_with = '' ORDER BY date", (INTERNAL,)):
        key = merchant_key(r["description"]) or normalize(r["description"])
        groups[(r["amount"] > 0, key)].append(r)

    found = []
    for (incoming, key), rows in groups.items():
        if len(rows) < MIN_OCCURRENCES:
            continue
        dates = [date.fromisoformat(r["date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if len(gaps) < MIN_OCCURRENCES - 1:
            continue
        cadence = _cadence(gaps)
        if not cadence:
            continue
        name, typical, per_month = cadence
        amounts = [abs(r["amount"]) for r in rows]
        med = median(amounts)
        stable = sum(abs(a - med) <= 0.2 * med for a in amounts) / len(amounts)
        last = dates[-1]
        found.append(dict(
            key=key, label=rows[-1]["description"][:50], direction="in" if incoming else "out",
            category=rows[-1]["category"], cadence=name, typical_amount=round(med, 2),
            monthly_equivalent=round(med * per_month, 2), occurrences=len(rows),
            last_date=last.isoformat(), next_date=(last + timedelta(days=round(typical))).isoformat(),
            variable=stable < 0.6, active=(today - last).days <= typical * 1.6))
    found.sort(key=lambda f: -f["monthly_equivalent"])
    return found


def summary(db: Database, today: date | None = None) -> dict:
    items = detect(db, today)
    active = [i for i in items if i["active"]]
    out = [i for i in active if i["direction"] == "out"]
    inc = [i for i in active if i["direction"] == "in"]
    return dict(
        items=items,
        monthly_costs=round(sum(i["monthly_equivalent"] for i in out), 2),
        monthly_income=round(sum(i["monthly_equivalent"] for i in inc), 2),
        yearly_costs=round(12 * sum(i["monthly_equivalent"] for i in out), 2),
        upcoming=sorted((i for i in out if i["next_date"] >= (today or date.today()).isoformat()),
                        key=lambda i: i["next_date"])[:6])
