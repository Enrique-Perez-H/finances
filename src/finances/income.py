"""What comes in: by type, by source and by month (internal transfers excluded)."""
from collections import defaultdict
from datetime import date

from .categorize import INTERNAL, merchant_key, normalize
from .db import Database


def summary(db: Database, months: int = 12, today: date | None = None) -> dict:
    """Completed months only: the current month is still filling up and would skew averages."""
    current = (today or date.today()).isoformat()[:7]
    rows = db.conn.execute(
        "SELECT date, amount, description, category FROM transactions"
        " WHERE amount > 0 AND category != ? AND paired_with = '' ORDER BY date", (INTERNAL,)).fetchall()
    all_months = sorted({r["date"][:7] for r in rows if r["date"][:7] < current})[-months:]
    rows = [r for r in rows if r["date"][:7] in all_months]

    by_cat = defaultdict(lambda: {"total": 0.0, "count": 0})
    by_source = defaultdict(lambda: {"total": 0.0, "count": 0, "last": "", "category": "", "label": ""})
    monthly = defaultdict(lambda: defaultdict(float))
    for r in rows:
        c = by_cat[r["category"]]
        c["total"] += r["amount"]
        c["count"] += 1
        key = merchant_key(r["description"]) or normalize(r["description"])
        s = by_source[key]
        s["total"] += r["amount"]
        s["count"] += 1
        s["last"] = r["date"]
        s["category"] = r["category"]
        s["label"] = r["description"][:50]
        monthly[r["date"][:7]][r["category"]] += r["amount"]

    n = max(len(all_months), 1)
    categories = sorted(
        (dict(category=k, total=round(v["total"], 2), count=v["count"],
              monthly_average=round(v["total"] / n, 2)) for k, v in by_cat.items()),
        key=lambda c: -c["total"])
    sources = sorted(
        (dict(label=v["label"], category=v["category"], total=round(v["total"], 2),
              count=v["count"], average=round(v["total"] / v["count"], 2), last=v["last"])
         for v in by_source.values()), key=lambda s: -s["total"])[:20]
    series = [dict(month=m, total=round(sum(monthly[m].values()), 2),
                   salary=round(monthly[m].get("Salary", 0), 2),
                   other=round(sum(monthly[m].values()) - monthly[m].get("Salary", 0), 2))
              for m in all_months]
    total = sum(c["total"] for c in categories)
    return dict(months=all_months, excluded_month=current, total=round(total, 2), monthly_average=round(total / n, 2),
                categories=categories, sources=sources, monthly=series)
