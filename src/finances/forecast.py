"""Forward projection of income, spending and wealth. An estimate, not advice.

Baseline comes from the last completed months: recurring income and costs as detected,
plus the average of everything else. Growth rates, extra items and the share of savings
that gets invested are assumptions you set.
"""
from datetime import date

from . import recurring
from .analysis import monthly_cashflow
from .db import Database


def _add_months(day: date, n: int) -> str:
    total = day.year * 12 + day.month - 1 + n
    return f"{total // 12}-{total % 12 + 1:02d}"


def _check(name: str, value: float, lo: float, hi: float) -> float:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def baseline(db: Database, today: date, lookback: int = 6) -> dict:
    current = today.isoformat()[:7]
    flow = [m for m in monthly_cashflow(db, lookback + 1) if m["month"] < current][-lookback:]
    n = len(flow)
    avg_in = sum(m["income"] for m in flow) / n if n else 0.0
    avg_out = sum(m["spending"] for m in flow) / n if n else 0.0
    rec = recurring.summary(db, today)
    rec_in = min(rec["monthly_income"], avg_in)
    rec_out = min(rec["monthly_costs"], avg_out)
    rows = db.conn.execute("SELECT kind, SUM(balance) b FROM accounts GROUP BY kind").fetchall()
    kinds = {r["kind"]: r["b"] for r in rows}
    return dict(
        months_used=n, income=round(avg_in, 2), spending=round(avg_out, 2),
        recurring_income=round(rec_in, 2), other_income=round(avg_in - rec_in, 2),
        recurring_costs=round(rec_out, 2), variable_costs=round(avg_out - rec_out, 2),
        liquid=round(kinds.get("cash", 0) + kinds.get("savings", 0), 2),
        invested=round(kinds.get("investment", 0), 2))


def project(db: Database, months: int = 24, income_growth: float = 0.0, inflation: float = 2.0,
            annual_return: float = 4.0, invest_share: float = 0.0, extra_income: float = 0.0,
            extra_cost: float = 0.0, include_other_income: bool = True,
            today: date | None = None) -> dict:
    """Percent arguments are in percent (3 means 3%)."""
    today = today or date.today()
    months = int(_check("months", months, 1, 120))
    _check("income_growth", income_growth, -50, 100)
    _check("inflation", inflation, -10, 100)
    _check("return", annual_return, -50, 100)
    _check("invest_share", invest_share, 0, 100)
    base = baseline(db, today)
    r_month = (1 + annual_return / 100) ** (1 / 12) - 1
    liquid, invested = base["liquid"], base["invested"]
    rows, runway = [], None
    for m in range(1, months + 1):
        income = (base["recurring_income"] * (1 + income_growth / 100) ** (m / 12)
                  + (base["other_income"] if include_other_income else 0) + extra_income)
        spending = ((base["recurring_costs"] + base["variable_costs"])
                    * (1 + inflation / 100) ** (m / 12) + extra_cost)
        net = income - spending
        invested *= 1 + r_month
        to_invest = net * invest_share / 100 if net > 0 else 0.0
        invested += to_invest
        liquid += net - to_invest
        if runway is None and liquid < 0:
            runway = m
        rows.append(dict(month=_add_months(today, m), income=round(income, 2),
                         spending=round(spending, 2), net=round(net, 2),
                         liquid=round(liquid, 2), invested=round(invested, 2),
                         total=round(liquid + invested, 2)))
    avg_net = sum(r["net"] for r in rows) / months
    return dict(baseline=base, rows=rows, runway_month=rows[runway - 1]["month"] if runway else None,
                average_net=round(avg_net, 2),
                savings_rate=round(avg_net / (sum(r["income"] for r in rows) / months), 4)
                if rows[0]["income"] else None,
                start_total=round(base["liquid"] + base["invested"], 2),
                end_total=rows[-1]["total"])
