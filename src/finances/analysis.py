"""All computations run locally against the in-memory database."""
from collections import defaultdict
from datetime import date

from .categorize import CAPITAL, INTERNAL, UNCATEGORIZED, merchant_key, normalize, suggest_pattern
from .db import Database


def net_worth(db: Database) -> dict:
    rows = db.conn.execute(
        "SELECT kind, SUM(balance) AS total FROM accounts WHERE kind != 'shared' GROUP BY kind").fetchall()
    by_kind = {r["kind"]: round(r["total"], 2) for r in rows}
    return {"total": round(sum(by_kind.values()), 2), "by_kind": by_kind}


def monthly_cashflow(db: Database, months: int = 6, today: date | None = None) -> list[dict]:
    """Income and spending per month, oldest first. The current month is flagged `partial`.
    Moves between your accounts, one-off capital items and reimbursement pairs are left out."""
    current = (today or date.today()).isoformat()[:7]
    rows = db.conn.execute(
        "SELECT substr(date,1,7) AS month,"
        " SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) AS income,"
        " SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) AS spending"
        " FROM counted WHERE category NOT IN (?, ?) AND paired_with = ''"
        " GROUP BY month ORDER BY month DESC LIMIT ?", (INTERNAL, CAPITAL, months)).fetchall()
    return [dict(month=r["month"], income=round(r["income"], 2), spending=round(r["spending"], 2),
                 saved=round(r["income"] - r["spending"], 2), partial=r["month"] == current)
            for r in reversed(rows)]


def average_month(db: Database, months: int | None = 12, today: date | None = None) -> dict:
    """What a typical month looks like: average spending per category over the last `months`
    completed months (None = all). Months without spending in a category count as zero.

    Costs you marked as recurring count at their monthly equivalent instead of as the raw entries: a
    yearly bill is a twelfth every month, a monthly one its average, so the average stays smooth."""
    from . import recurring  # imported here: recurring reads the same tables
    current = (today or date.today()).isoformat()[:7]
    available = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM transactions WHERE substr(date,1,7) < ? ORDER BY m DESC",
        (current,))]
    used = available[:months] if months else available
    if not used:
        return dict(months_used=0, first=None, last=None, total=0.0, categories=[], recurring=[], recurring_total=0.0)
    manual = [g for g in recurring._manual_rows(db).values() if g["direction"] == "out" and g["entries"]]
    claimed = {e["id"] for g in manual for e in g["entries"]}
    marks = ",".join("?" * len(used))
    totals, active = defaultdict(float), defaultdict(set)
    for r in db.conn.execute(
            f"SELECT id, category, amount, substr(date,1,7) AS m FROM counted WHERE amount<0"
            f" AND category NOT IN (?, ?) AND paired_with = '' AND substr(date,1,7) IN ({marks})",
            (INTERNAL, CAPITAL, *used)):
        if r["id"] not in claimed:
            totals[r["category"]] += -r["amount"]
            active[r["category"]].add(r["m"])
    n = len(used)
    items, recurring_by_category = [], defaultdict(float)
    for g in manual:
        item = recurring._manual_item(g, today or date.today(), used)
        if item["monthly_equivalent"] > 0:
            items.append(dict(name=item["label"], monthly=item["monthly_equivalent"], cadence=item["cadence"],
                              category=item["category"]))
            recurring_by_category[item["category"]] += item["monthly_equivalent"]
    for category, monthly in recurring_by_category.items():
        totals[category] += monthly * n  # spread evenly over the months
        active[category] |= set(used)
    grand = sum(totals.values())
    categories = [dict(category=c, average=round(t / n, 2), total=round(t, 2), active_months=len(active[c]),
                       share=round(t / grand, 4) if grand else 0,
                       recurring_part=round(recurring_by_category.get(c, 0.0), 2))
                  for c, t in sorted(totals.items(), key=lambda kv: -kv[1])]
    items.sort(key=lambda i: -i["monthly"])
    return dict(months_used=n, first=min(used), last=max(used), total=round(grand / n, 2), categories=categories,
                recurring=items, recurring_total=round(sum(i["monthly"] for i in items), 2))


def spending_by_category(db: Database, month: str | None = None) -> list[dict]:
    month = month or date.today().isoformat()[:7]
    rows = db.conn.execute(
        "SELECT category, SUM(-amount) AS total FROM counted"
        " WHERE amount<0 AND category NOT IN (?, ?) AND paired_with = '' AND substr(date,1,7)=?"
        " GROUP BY category ORDER BY total DESC", (INTERNAL, CAPITAL, month)).fetchall()
    return [dict(category=r["category"], total=round(r["total"], 2)) for r in rows]


def projection(db: Database, years: int = 10, annual_return: float = 0.04) -> list[dict]:
    """Compound current net worth plus the average monthly saving. An estimate only."""
    flow = monthly_cashflow(db, 6)
    saved = sum(m["saved"] for m in flow) / len(flow) if flow else 0.0
    value = net_worth(db)["total"]
    monthly_rate = (1 + annual_return) ** (1 / 12) - 1
    out = []
    for month in range(1, years * 12 + 1):
        value = value * (1 + monthly_rate) + saved
        if month % 12 == 0:
            out.append(dict(year=month // 12, value=round(value, 2)))
    return out


def summary(db: Database) -> dict:
    holdings = db.conn.execute(
        "SELECT symbol, SUM(quantity) AS quantity, SUM(value) AS value,"
        " SUM(cost_basis) AS cost FROM holdings GROUP BY symbol").fetchall()
    accounts = db.conn.execute(
        "SELECT id, provider, name, kind, currency, balance, updated_at FROM accounts WHERE kind != 'shared'"
        " ORDER BY balance DESC").fetchall()
    return {
        "net_worth": net_worth(db),
        "accounts": [dict(a) for a in accounts],
        "cashflow": monthly_cashflow(db),
        "categories": spending_by_category(db),
        "holdings": [dict(h) for h in holdings],
        "projection": projection(db),
    }


def spending(db: Database, month: str | None = None) -> dict:
    """One month by category, with the previous month for comparison."""
    months = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM transactions ORDER BY m DESC")]
    month = month if month in months else (months[0] if months else date.today().isoformat()[:7])
    idx = months.index(month) if month in months else -1
    prev_month = months[idx + 1] if 0 <= idx + 1 < len(months) else None
    cur = {c["category"]: c["total"] for c in spending_by_category(db, month)}
    prev = {c["category"]: c["total"] for c in spending_by_category(db, prev_month)} if prev_month else {}
    total = sum(cur.values())
    cats = [dict(category=c, total=t, share=round(t / total, 4) if total else 0,
                 previous=prev.get(c, 0)) for c, t in cur.items()]
    unclear = db.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category=? AND paired_with=''", (UNCATEGORIZED,)).fetchone()[0]
    return dict(month=month, months=months, previous_month=prev_month, total=round(total, 2),
                previous_total=round(sum(prev.values()), 2), categories=cats, unclear=unclear)


def transactions(db: Database, category: str | None, month: str | None, limit: int = 200) -> list[dict]:
    sql, args = ("SELECT id, date, amount, description, category, cat_source, share, raw_amount FROM counted"
                 " WHERE paired_with = ''"), []
    if category:
        sql += " AND category=?"
        args.append(category)
    if month:
        sql += " AND substr(date,1,7)=?"
        args.append(month)
    sql += " ORDER BY date DESC, id LIMIT ?"
    args.append(limit)
    out = [dict(r) for r in db.conn.execute(sql, args)]
    for t in out:  # how a rule for this entry would be worded, so the UI can offer "move all like this"
        t["key"] = merchant_key(t["description"]) or normalize(t["description"])
        t["pattern"] = suggest_pattern(t["key"], t["description"])
    return out


def entries(db: Database, category: str | None = None, since: str | None = None,
            until: str | None = None, q: str | None = None, sort: str = "date",
            offset: int = 0, limit: int = 50, direction: str | None = None) -> dict:
    """Browse entries: filter by category (None or '*' = all), dates, text (accent-insensitive) and
    direction ('out' = spending only, 'in' = receipts only).
    Reimbursement pairs are left out, they have their own list."""
    sql, args = ("SELECT id, date, amount, description, category, cat_source, share, raw_amount FROM counted"
                 " WHERE paired_with = ''"), []
    if category and category != "*":
        sql += " AND category=?"
        args.append(category)
    if since:
        sql += " AND date >= ?"
        args.append(since)
    if until:
        sql += " AND date <= ?"
        args.append(until)
    if direction in ("out", "in"):
        sql += " AND raw_amount < 0" if direction == "out" else " AND raw_amount > 0"
    sql += " ORDER BY " + ("ABS(amount) DESC, date DESC" if sort == "amount" else "date DESC") + ", id"
    needle = normalize(q or "")
    rows = [dict(r) for r in db.conn.execute(sql, args)
            if not needle or needle in normalize(r["description"])]
    total_in = round(sum(r["amount"] for r in rows if r["amount"] > 0), 2)
    total_out = round(-sum(r["amount"] for r in rows if r["amount"] < 0), 2)
    limit = max(1, min(limit, 200))
    page = rows[max(offset, 0):max(offset, 0) + limit]
    for t in page:  # how a rule for this entry would be worded ("move all like this")
        t["key"] = merchant_key(t["description"]) or normalize(t["description"])
        t["pattern"] = suggest_pattern(t["key"], t["description"])
    return dict(items=page, count=len(rows), total_in=total_in, total_out=total_out,
                has_more=max(offset, 0) + limit < len(rows))


def top_entries(db: Database, month: str, n: int = 3) -> dict:
    """The biggest single spends and incomes of one month, counted like the monthly totals
    (own-account moves, one-off capital items and reimbursed pairs are left out)."""
    if len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit():
        raise ValueError("month must look like 2026-05")
    n = max(1, min(n, 20))

    def pick(sign: str, order: str):
        rows = db.conn.execute(
            f"SELECT date, amount, description, category FROM counted"
            f" WHERE substr(date,1,7)=? AND amount {sign} 0 AND category NOT IN (?, ?)"
            f" AND paired_with='' ORDER BY amount {order}, date, id LIMIT ?",
            (month, INTERNAL, CAPITAL, n)).fetchall()
        return [dict(r) for r in rows]

    return dict(month=month, spends=pick("<", "ASC"), incomes=pick(">", "DESC"))
