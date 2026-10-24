"""Savings fund calculator (e.g. for a child): contributions, interest, optional tax.

Model: annual_rate is an *effective* yearly rate, applied monthly as (1+rate)^(1/12)-1.
Each month the balance earns interest, then the contribution is added (end of month).
The monthly contribution grows by `contribution_growth` % once a year. Percent values
are in percent (3 means 3%). Tax, if any, is a flat rate on the total gain, at the end.
"""
from datetime import date

from .db import Database

FIELDS = ("name", "start_date", "initial", "monthly", "annual_rate", "years",
          "contribution_growth", "tax_rate", "target", "link_account_id")


def clean(body: dict, db: Database | None = None) -> dict:
    """Validate and normalise a plan from the UI."""
    name = str(body.get("name", "")).strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("name must be 1 to 60 characters")

    def num(key, lo, hi, default=None):
        raw = body.get(key, default)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return v

    try:
        start = date.fromisoformat(str(body.get("start_date") or date.today().isoformat()))
    except ValueError:
        raise ValueError("start_date must be YYYY-MM-DD") from None
    target = body.get("target")
    link = body.get("link_account_id") or None
    if link and db is not None and not db.conn.execute(
            "SELECT 1 FROM accounts WHERE id=?", (link,)).fetchone():
        raise ValueError("linked account not found")
    return dict(
        name=name, start_date=start.isoformat(), initial=num("initial", 0, 1e9, 0),
        monthly=num("monthly", 0, 1e7, 0), annual_rate=num("annual_rate", -50, 100, 3),
        years=int(num("years", 1, 40, 18)), contribution_growth=num("contribution_growth", 0, 50, 0),
        tax_rate=num("tax_rate", 0, 60, 0),
        target=num("target", 0, 1e10) if target not in (None, "") else None, link_account_id=link)


def _simulate(initial, monthly, rate, years, growth):
    r = (1 + rate / 100) ** (1 / 12) - 1
    balance, contributed, rows = initial, initial, []
    for t in range(1, years * 12 + 1):
        contribution = monthly * (1 + growth / 100) ** ((t - 1) // 12)
        balance = balance * (1 + r) + contribution
        contributed += contribution
        if t % 12 == 0:
            rows.append((t // 12, balance, contributed))
    return rows


def _required_monthly(plan, initial):
    """Monthly contribution that reaches the target, given the other assumptions."""
    base = _simulate(initial, 0, plan["annual_rate"], plan["years"], 0)[-1][1]
    per_euro = _simulate(0, 1, plan["annual_rate"], plan["years"], plan["contribution_growth"])[-1][1]
    return max(0.0, (plan["target"] - base) / per_euro) if per_euro else None


def project(plan: dict, linked_balance: float | None = None) -> dict:
    initial = linked_balance if linked_balance is not None else plan["initial"]
    start_year = date.fromisoformat(plan["start_date"]).year

    def run(rate):
        return _simulate(initial, plan["monthly"], rate, plan["years"], plan["contribution_growth"])

    rows = run(plan["annual_rate"])
    years = [dict(year=start_year + y, age=y, balance=round(b, 2), contributed=round(c, 2),
                  interest=round(b - c, 2)) for y, b, c in rows]
    final, contributed = rows[-1][1], rows[-1][2]
    interest = final - contributed
    tax = max(interest, 0) * plan["tax_rate"] / 100
    return dict(
        initial=round(initial, 2), years=years, final=round(final, 2),
        contributed=round(contributed, 2), interest=round(interest, 2),
        tax=round(tax, 2), final_after_tax=round(final - tax, 2),
        scenarios=[dict(rate=plan["annual_rate"] + d, final=round(run(plan["annual_rate"] + d)[-1][1], 2))
                   for d in (-1, 0, 1)],
        required_monthly=(round(_required_monthly(plan, initial), 2)
                          if plan.get("target") is not None else None),
        target_reached=(plan["target"] is not None and final >= plan["target"]))


def _linked(db: Database, plan: dict):
    if not plan.get("link_account_id"):
        return None
    row = db.conn.execute("SELECT balance FROM accounts WHERE id=?", (plan["link_account_id"],)).fetchone()
    return row["balance"] if row else None


def list_plans(db: Database) -> list[dict]:
    out = []
    for r in db.conn.execute("SELECT * FROM fund_plans ORDER BY id"):
        plan = dict(r)
        out.append(dict(plan=plan, projection=project(plan, _linked(db, plan))))
    return out


def save_plan(db: Database, body: dict) -> dict:
    plan = clean(body, db)
    cols = ", ".join(FIELDS)
    marks = ", ".join(f":{f}" for f in FIELDS)
    if body.get("id"):
        sets = ", ".join(f"{f}=:{f}" for f in FIELDS)
        cur = db.conn.execute(f"UPDATE fund_plans SET {sets} WHERE id=:id", {**plan, "id": int(body["id"])})
        if not cur.rowcount:
            raise ValueError("unknown plan")
        plan["id"] = int(body["id"])
    else:
        cur = db.conn.execute(f"INSERT INTO fund_plans({cols}) VALUES({marks})", plan)
        plan["id"] = cur.lastrowid
    return plan


def delete_plan(db: Database, plan_id) -> None:
    db.conn.execute("DELETE FROM fund_plans WHERE id=?", (int(plan_id),))


def preview(db: Database, body: dict) -> dict:
    plan = clean(body, db)
    return project(plan, _linked(db, plan))
