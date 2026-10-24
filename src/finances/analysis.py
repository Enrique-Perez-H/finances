"""All computations run locally against the in-memory database."""
from datetime import date

from .db import Database


def net_worth(db: Database) -> dict:
    rows = db.conn.execute(
        "SELECT kind, SUM(balance) AS total FROM accounts GROUP BY kind").fetchall()
    by_kind = {r["kind"]: round(r["total"], 2) for r in rows}
    return {"total": round(sum(by_kind.values()), 2), "by_kind": by_kind}


def monthly_cashflow(db: Database, months: int = 6) -> list[dict]:
    rows = db.conn.execute(
        "SELECT substr(date,1,7) AS month,"
        " SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) AS income,"
        " SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) AS spending"
        " FROM transactions GROUP BY month ORDER BY month DESC LIMIT ?", (months,)).fetchall()
    return [dict(month=r["month"], income=round(r["income"], 2),
                 spending=round(r["spending"], 2),
                 saved=round(r["income"] - r["spending"], 2)) for r in reversed(rows)]


def spending_by_category(db: Database, month: str | None = None) -> list[dict]:
    month = month or date.today().isoformat()[:7]
    rows = db.conn.execute(
        "SELECT category, SUM(-amount) AS total FROM transactions"
        " WHERE amount<0 AND substr(date,1,7)=? GROUP BY category ORDER BY total DESC",
        (month,)).fetchall()
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
        "SELECT id, provider, name, kind, currency, balance, updated_at FROM accounts"
        " ORDER BY balance DESC").fetchall()
    return {
        "net_worth": net_worth(db),
        "accounts": [dict(a) for a in accounts],
        "cashflow": monthly_cashflow(db),
        "categories": spending_by_category(db),
        "holdings": [dict(h) for h in holdings],
        "projection": projection(db),
    }
