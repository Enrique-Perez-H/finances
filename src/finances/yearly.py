"""Per-year totals (for the tax return and your own review) and CSV export.

Counted exactly like the monthly views: moves between your own accounts, one-off capital items
and reimbursed pairs are shown separately and left out of income and spending. Informational:
check the figures against your bank and broker certificates, this is not tax advice.
"""
import csv
import io
from collections import defaultdict

from .categorize import CAPITAL, EXCLUDED, INTERNAL, merchant_key, normalize
from .db import Database

INTEREST = "Interest & dividends"
TAXES = "Taxes & fees"
INTEREST_MARKER = "tr interest"  # Trade Republic marks its cash-interest payouts


def years(db: Database) -> list[str]:
    return [r[0] for r in db.conn.execute(
        "SELECT DISTINCT substr(date,1,4) y FROM transactions ORDER BY y DESC")]


def _rows(db: Database, year: str) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT t.id, t.date, t.amount, t.description, t.category, t.cat_source, t.paired_with,"
        " a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE substr(t.date,1,4)=? ORDER BY t.date, t.id", (year,))]


def _counted(r: dict) -> bool:
    return r["category"] not in EXCLUDED and not r["paired_with"]


def summary(db: Database, year: str) -> dict:
    rows = _rows(db, year)
    income, spending = defaultdict(float), defaultdict(float)
    sources = defaultdict(lambda: {"total": 0.0, "count": 0, "label": "", "category": ""})
    interest = defaultdict(lambda: {"interest": 0.0, "other": 0.0})
    monthly = {f"{year}-{m:02d}": {"income": 0.0, "spending": 0.0} for m in range(1, 13)}
    taxes, not_counted = [], {"internal_in": 0.0, "internal_out": 0.0, "capital_in": 0.0,
                              "capital_out": 0.0, "reimbursed_spent": 0.0, "reimbursed_received": 0.0,
                              "reimbursed_pairs": 0}
    for r in rows:
        amount = r["amount"]
        if not _counted(r):
            if r["category"] == INTERNAL:
                not_counted["internal_in" if amount > 0 else "internal_out"] += abs(amount)
            elif r["category"] == CAPITAL:
                not_counted["capital_in" if amount > 0 else "capital_out"] += abs(amount)
            elif r["paired_with"]:
                if amount < 0:
                    not_counted["reimbursed_spent"] += -amount
                    not_counted["reimbursed_pairs"] += 1
                else:
                    not_counted["reimbursed_received"] += amount
            continue
        month = r["date"][:7]
        if amount > 0:
            income[r["category"]] += amount
            monthly[month]["income"] += amount
            key = merchant_key(r["description"]) or normalize(r["description"])
            s = sources[key]
            s["total"] += amount
            s["count"] += 1
            s["label"], s["category"] = r["description"][:50], r["category"]
            if r["category"] == INTEREST:
                kind = "interest" if INTEREST_MARKER in normalize(r["description"]) else "other"
                interest[r["account"]][kind] += amount
        else:
            spending[r["category"]] += -amount
            monthly[month]["spending"] += -amount
            if r["category"] == TAXES:
                taxes.append(dict(date=r["date"], description=r["description"][:50], amount=-amount))
    total_in, total_out = sum(income.values()), sum(spending.values())

    def ordered(d):
        return [dict(category=k, total=round(v, 2)) for k, v in sorted(d.items(), key=lambda kv: -kv[1])]

    taxes.sort(key=lambda t: -t["amount"])
    return dict(
        year=year, years=years(db),
        income=dict(total=round(total_in, 2), by_category=ordered(income)),
        spending=dict(total=round(total_out, 2), by_category=ordered(spending)),
        saved=round(total_in - total_out, 2),
        savings_rate=round((total_in - total_out) / total_in, 4) if total_in else None,
        interest=dict(
            total=round(sum(v["interest"] + v["other"] for v in interest.values()), 2),
            interest=round(sum(v["interest"] for v in interest.values()), 2),
            other=round(sum(v["other"] for v in interest.values()), 2),
            accounts=[dict(account=k, interest=round(v["interest"], 2), other=round(v["other"], 2))
                      for k, v in sorted(interest.items())]),
        taxes_paid=dict(total=round(sum(t["amount"] for t in taxes), 2), top=taxes[:5]),
        income_sources=[dict(label=v["label"], category=v["category"], total=round(v["total"], 2), count=v["count"])
                        for v in sorted(sources.values(), key=lambda s: -s["total"])[:10]],
        not_counted={k: round(v, 2) if isinstance(v, float) else v for k, v in not_counted.items()},
        monthly=[dict(month=m, income=round(v["income"], 2), spending=round(v["spending"], 2),
                      saved=round(v["income"] - v["spending"], 2)) for m, v in monthly.items()])


# --- CSV ---------------------------------------------------------------------------------

def _safe(text: str) -> str:
    """Stop spreadsheets from running a bank description that starts like a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _number(value: float, style: str) -> str:
    text = f"{value:.2f}"
    return text.replace(".", ",") if style == "es" else text


def _write(header: list[str], lines: list[list], style: str) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";" if style == "es" else ",", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(lines)
    text = out.getvalue()
    return (("﻿" + text) if style == "es" else text).encode("utf-8")  # BOM: Excel reads accents


def export_entries(db: Database, year: str, style: str = "es") -> bytes:
    """Every entry of the year (or all years), with whether and why it is counted."""
    if year == "all":
        rows = [dict(r) for r in db.conn.execute(
            "SELECT t.id, t.date, t.amount, t.description, t.category, t.cat_source, t.paired_with,"
            " a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id"
            " ORDER BY t.date, t.id")]
    else:
        rows = _rows(db, year)
    lines = []
    for r in rows:
        reason = "" if _counted(r) else (r["category"] if r["category"] in EXCLUDED else "Reimbursed")
        lines.append([r["date"], _safe(r["account"]), _safe(r["description"]), _number(r["amount"], style),
                      _safe(r["category"]), "yes" if _counted(r) else "no", reason,
                      {"manual": "set by you", "user": "your rule", "connector": "provider",
                       "default": "built-in rule"}.get(r["cat_source"], "automatic" if r["cat_source"] else "")])
    return _write(["Date", "Account", "Description", "Amount", "Category", "Counted", "Not counted because",
                   "Category set by"], lines, style)


def export_summary(db: Database, year: str, style: str = "es") -> bytes:
    s = summary(db, year)
    lines = [["Total", "Income", _number(s["income"]["total"], style)],
             ["Total", "Spending", _number(s["spending"]["total"], style)],
             ["Total", "Saved", _number(s["saved"], style)]]
    lines += [["Income by type", c["category"], _number(c["total"], style)] for c in s["income"]["by_category"]]
    lines += [["Spending by category", c["category"], _number(c["total"], style)] for c in s["spending"]["by_category"]]
    lines += [["Interest and dividends", a["account"] + " (interest)", _number(a["interest"], style)] for a in s["interest"]["accounts"]]
    lines += [["Interest and dividends", a["account"] + " (other)", _number(a["other"], style)] for a in s["interest"]["accounts"]]
    lines += [["Taxes and fees paid", "Total", _number(s["taxes_paid"]["total"], style)]]
    lines += [["Not counted", k, _number(v, style) if isinstance(v, float) else str(v)] for k, v in s["not_counted"].items()]
    lines += [["Month", m["month"], _number(m["saved"], style)] for m in s["monthly"]]
    return _write(["Section", "Item", "Amount"], [[_safe(str(c)) if i < 2 else c for i, c in enumerate(l)] for l in lines], style)
