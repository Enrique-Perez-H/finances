"""Detect recurring payments and income from transaction history.

Entries are grouped by merchant, split by direction. Two detectors run:

* Cadence: at least 3 occurrences whose gaps mostly match one rhythm (weekly, monthly, every
  two months, quarterly...). Dates may drift by a few days. Amounts that vary a lot (utility
  bills) are flagged `variable`.
* Presence: payees that show up in most months with no fixed day or amount, e.g. a cleaning
  service billed every month with a different invoice date and total. A payee counts when it
  appears in at least 5 of the last 6 completed months (or 75% of the last 12), no more than
  about twice a month: shops visited several times a month are shopping, not a bill.

Items you reject are remembered and left out.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median

from .categorize import CAPITAL, GENERIC_PATTERNS, INTERNAL, merchant_key, normalize
from .db import Database

# (name, min days, max days, typical days, payments per month)
CADENCES = [
    ("weekly", 5, 9, 7, 52 / 12),
    ("biweekly", 12, 17, 14, 26 / 12),
    ("monthly", 25, 37, 30.44, 1.0),
    ("every two months", 52, 70, 61, 0.5),
    ("quarterly", 80, 102, 91, 1 / 3),
    ("semiannual", 165, 200, 182, 1 / 6),
    ("yearly", 340, 390, 365, 1 / 12),
]
MIN_OCCURRENCES = 3
MIN_GAP_MATCH = 0.65
# presence detector
WINDOW_MONTHS = 12
MIN_HISTORY_MONTHS = 6
SEEN_IN_LAST_SIX = 5
SEEN_SHARE_LONG = 0.75
MAX_MEDIAN_PER_MONTH = 2
MAX_PER_MONTH = 4
# Things you choose to buy: visiting the same shop most months does not make it a bill.
# (A steady rhythm is still detected for these by the cadence detector.)
DISCRETIONARY = {"Groceries", "Restaurants & bars", "Shopping", "Leisure", "Travel",
                 "Cash withdrawals", "Transport"}


def _cadence(gaps: list[int]):
    med = median(gaps)
    for name, lo, hi, typical, per_month in CADENCES:
        if lo <= med <= hi:
            ok = sum(lo <= g <= hi for g in gaps) / len(gaps)
            if ok >= MIN_GAP_MATCH:
                return name, typical, per_month
    return None


def _groups(db: Database, exclude: set[str] = frozenset()):
    groups = defaultdict(list)
    for r in db.conn.execute(
            "SELECT id, date, amount, description, category FROM transactions"
            " WHERE category NOT IN (?, ?) AND amount != 0 AND paired_with = '' ORDER BY date",
            (INTERNAL, CAPITAL)):
        if r["id"] in exclude:
            continue
        key = merchant_key(r["description"]) or normalize(r["description"])
        groups[(r["amount"] > 0, key)].append(r)
    return groups


# --- costs (or incomes) you mark as recurring yourself -----------------------------------------

MANUAL_CADENCES = {"monthly": 30.44, "every two months": 61, "quarterly": 91, "semiannual": 182, "yearly": 365}


def _manual_rows(db: Database) -> dict[int, dict]:
    """Each group with its entries: the ones you picked plus later ones matching its name variants."""
    groups = {r["id"]: dict(r, patterns=[], entries=[]) for r in db.conn.execute("SELECT * FROM recurring_manual")}
    for r in db.conn.execute("SELECT group_id, pattern FROM recurring_manual_patterns ORDER BY pattern"):
        if r["group_id"] in groups:
            groups[r["group_id"]]["patterns"].append(r["pattern"])
    linked = defaultdict(set)
    for r in db.conn.execute("SELECT group_id, tx_id FROM recurring_manual_entries"):
        linked[r["group_id"]].add(r["tx_id"])
    matchers = {gid: [(p, normalize(p)) for p in g["patterns"]] for gid, g in groups.items()}
    for r in db.conn.execute(
            "SELECT id, date, amount, description, category FROM transactions"
            " WHERE category NOT IN (?, ?) AND amount != 0 AND paired_with = '' ORDER BY date, id",
            (INTERNAL, CAPITAL)):
        text = None
        for gid, g in groups.items():
            wanted = r["amount"] > 0 if g["direction"] == "in" else r["amount"] < 0
            hit = r["id"] in linked[gid]
            if not hit and wanted and matchers[gid]:
                text = text if text is not None else normalize(r["description"])
                hit = any(f" {p} " in f" {text} " for _, p in matchers[gid])
            if hit:
                g["entries"].append(dict(r))
    return groups


def _manual_item(g: dict, today: date, window: list[str]) -> dict:
    rows = g["entries"]
    typical = MANUAL_CADENCES.get(g["cadence"], 30.44)
    base = dict(key=f"manual:{g['id']}", group_id=g["id"], label=g["name"], direction=g["direction"],
                cadence=g["cadence"], kind="manual", patterns=g["patterns"], months_seen=None)
    if not rows:
        return dict(base, category="", typical_amount=0.0, amount_min=0.0, amount_max=0.0, monthly_equivalent=0.0,
                    occurrences=0, last_date=None, next_date=None, variable=False, active=False, entries=[])
    amounts = [abs(r["amount"]) for r in rows]
    med = median(amounts)
    stable = sum(abs(a - med) <= 0.2 * med for a in amounts) / len(amounts)
    last = date.fromisoformat(rows[-1]["date"])
    first_month = rows[0]["date"][:7]
    span = [m for m in window if m >= first_month]  # completed months since it started (at most 12)
    if span:
        monthly = sum(abs(r["amount"]) for r in rows if r["date"][:7] in span) / len(span)
    else:  # it only exists in the month in progress: no average yet
        monthly = med * 30.44 / typical
    category = Counter(r["category"] for r in rows).most_common(1)[0][0]
    return dict(base, category=category, typical_amount=round(med, 2), amount_min=round(min(amounts), 2),
                amount_max=round(max(amounts), 2), monthly_equivalent=round(monthly, 2), occurrences=len(rows),
                last_date=last.isoformat(), next_date=(last + timedelta(days=round(typical))).isoformat(),
                variable=stable < 0.6,
                active=(today - last).days <= typical * (2.1 if g["cadence"] == "monthly" else 1.6),
                entries=[dict(id=r["id"], date=r["date"], amount=r["amount"], description=r["description"][:60])
                         for r in rows])


def _clean_name(name) -> str:
    name = str(name or "").strip()
    if not 1 <= len(name) <= 60:
        raise ValueError("give it a name of 1 to 60 characters")
    return name


def _clean_cadence(cadence) -> str:
    if cadence not in MANUAL_CADENCES:
        raise ValueError("cadence must be one of: " + ", ".join(MANUAL_CADENCES))
    return cadence


def _clean_pattern(pattern) -> str:
    pattern = normalize(str(pattern or ""))
    if len(pattern) < 3 or pattern in GENERIC_PATTERNS:
        raise ValueError("use at least 3 letters of the payee's name (not a word like 'transferencia')")
    return pattern


def mark(db: Database, tx_ids: list[str], group_id: int | None = None, name: str | None = None,
         cadence: str = "monthly", pattern: str | None = None, direction: str | None = None) -> int:
    """Mark entries as one recurring cost: into an existing group, or a new one with a name.
    Passing a pattern also makes later entries with that name join automatically."""
    tx_ids = [str(i) for i in tx_ids or []]
    if len(tx_ids) > 500:
        raise ValueError("select at most 500 entries")
    rows = []
    if tx_ids:
        marks = ",".join("?" * len(tx_ids))
        rows = db.conn.execute(f"SELECT id, amount FROM transactions WHERE id IN ({marks})", tx_ids).fetchall()
        if len(rows) != len(set(tx_ids)):
            raise ValueError("some of those entries no longer exist")
        if not (all(r["amount"] < 0 for r in rows) or all(r["amount"] > 0 for r in rows)):
            raise ValueError("choose only payments or only receipts, not both")
    if group_id is not None:
        group = db.conn.execute("SELECT * FROM recurring_manual WHERE id=?", (int(group_id),)).fetchone()
        if not group:
            raise ValueError("unknown recurring item")
        gid = group["id"]
        if rows and (rows[0]["amount"] > 0) != (group["direction"] == "in"):
            raise ValueError("those entries go the other way from this recurring item")
    else:
        if not rows and not pattern:
            raise ValueError("select at least one entry, or give a name to match")
        if rows:
            direction = "in" if rows[0]["amount"] > 0 else "out"
        elif direction not in ("in", "out"):
            direction = "out"  # matching by name alone: a cost unless you say it is income
        gid = db.conn.execute("INSERT INTO recurring_manual(name, direction, cadence) VALUES(?,?,?)",
                              (_clean_name(name), direction, _clean_cadence(cadence))).lastrowid
    for r in rows:
        db.conn.execute("INSERT OR IGNORE INTO recurring_manual_entries(group_id, tx_id) VALUES(?,?)", (gid, r["id"]))
    if pattern:
        db.conn.execute("INSERT OR IGNORE INTO recurring_manual_patterns(group_id, pattern) VALUES(?,?)",
                        (gid, _clean_pattern(pattern)))
    return gid


def update_manual(db: Database, group_id: int, name=None, cadence=None) -> None:
    if not db.conn.execute("SELECT 1 FROM recurring_manual WHERE id=?", (int(group_id),)).fetchone():
        raise ValueError("unknown recurring item")
    if name is not None:
        db.conn.execute("UPDATE recurring_manual SET name=? WHERE id=?", (_clean_name(name), int(group_id)))
    if cadence is not None:
        db.conn.execute("UPDATE recurring_manual SET cadence=? WHERE id=?", (_clean_cadence(cadence), int(group_id)))


def add_pattern(db: Database, group_id: int, pattern: str) -> None:
    if not db.conn.execute("SELECT 1 FROM recurring_manual WHERE id=?", (int(group_id),)).fetchone():
        raise ValueError("unknown recurring item")
    db.conn.execute("INSERT OR IGNORE INTO recurring_manual_patterns(group_id, pattern) VALUES(?,?)",
                    (int(group_id), _clean_pattern(pattern)))


def remove_pattern(db: Database, group_id: int, pattern: str) -> None:
    db.conn.execute("DELETE FROM recurring_manual_patterns WHERE group_id=? AND pattern=?",
                    (int(group_id), normalize(pattern)))


def unlink(db: Database, group_id: int, tx_id: str) -> None:
    db.conn.execute("DELETE FROM recurring_manual_entries WHERE group_id=? AND tx_id=?", (int(group_id), str(tx_id)))


def delete_manual(db: Database, group_id: int) -> None:
    for table in ("recurring_manual_entries", "recurring_manual_patterns"):
        db.conn.execute(f"DELETE FROM {table} WHERE group_id=?", (int(group_id),))
    db.conn.execute("DELETE FROM recurring_manual WHERE id=?", (int(group_id),))


def _window(db: Database, today: date) -> list[str]:
    """The last completed months that have any transactions (the month in progress is left out)."""
    current = today.isoformat()[:7]
    months = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM transactions WHERE substr(date,1,7) < ? ORDER BY m", (current,))]
    return months[-WINDOW_MONTHS:]


def _presence(rows: list[dict], window: list[str]) -> dict:
    """How a payee shows up month by month, and whether that looks like a recurring one."""
    counts = Counter(r["date"][:7] for r in rows if r["date"][:7] in window)
    seen_all = sum(1 for m in window if counts[m])
    last6 = window[-6:]
    seen6 = sum(1 for m in last6 if counts[m])
    present = [counts[m] for m in window if counts[m]]
    per_month = median(present) if present else 0
    reasons = []
    if rows and rows[-1]["category"] in DISCRETIONARY:
        reasons.append(f"{rows[-1]['category']} is discretionary spending, not a bill")
    if len(window) < MIN_HISTORY_MONTHS:
        reasons.append(f"needs {MIN_HISTORY_MONTHS} completed months of history, has {len(window)}")
    if not (seen6 >= SEEN_IN_LAST_SIX or (len(window) >= 10 and seen_all / len(window) >= SEEN_SHARE_LONG)):
        reasons.append(f"seen in {seen6} of the last {len(last6)} months and {seen_all} of {len(window)}; "
                       f"needs {SEEN_IN_LAST_SIX} of the last 6, or {int(SEEN_SHARE_LONG * 100)}% of the last 12")
    if present and (per_month > MAX_MEDIAN_PER_MONTH or max(present) > MAX_PER_MONTH):
        reasons.append(f"appears {per_month:g} times in a typical month (up to {max(present)}): that is shopping, not a bill")
    return dict(seen_all=seen_all, seen6=seen6, window=len(window), per_month=per_month,
                counts=dict(sorted(counts.items())), ok=not reasons, reasons=reasons)


def _item(key, incoming, rows, name, per_month_factor, typical_days, kind, today, window, presence=None):
    amounts = [abs(r["amount"]) for r in rows]
    med = median(amounts)
    stable = sum(abs(a - med) <= 0.2 * med for a in amounts) / len(amounts)
    last = date.fromisoformat(rows[-1]["date"])
    in_window = [abs(r["amount"]) for r in rows if r["date"][:7] in window]
    if kind == "presence" and window:
        monthly = sum(in_window) / len(window)  # honest for bills that change every month
    else:
        monthly = (sum(amounts) / len(amounts) if stable < 0.6 else med) * per_month_factor
    return dict(
        key=key, label=rows[-1]["description"][:50], direction="in" if incoming else "out",
        category=rows[-1]["category"], cadence=name, kind=kind, typical_amount=round(med, 2),
        amount_min=round(min(amounts), 2), amount_max=round(max(amounts), 2),
        monthly_equivalent=round(monthly, 2), occurrences=len(rows), last_date=last.isoformat(),
        next_date=(last + timedelta(days=round(typical_days))).isoformat(), variable=stable < 0.6,
        active=(today - last).days <= typical_days * (2.1 if kind == "presence" else 1.6),  # irregular dates: allow a skipped month
        months_seen=(f"{presence['seen_all']} of the last {presence['window']} months" if presence else None))


def ignored(db: Database) -> set[tuple[str, str]]:
    return {(r["key"], r["direction"]) for r in db.conn.execute("SELECT key, direction FROM recurring_ignored")}


def ignore(db: Database, key: str, direction: str) -> None:
    if direction not in ("in", "out") or not key.strip():
        raise ValueError("key and direction (in or out) are required")
    db.conn.execute("INSERT OR IGNORE INTO recurring_ignored(key, direction) VALUES(?,?)", (key.strip(), direction))


def restore(db: Database, key: str, direction: str) -> None:
    db.conn.execute("DELETE FROM recurring_ignored WHERE key=? AND direction=?", (key, direction))


def detect(db: Database, today: date | None = None, include_ignored: bool = False) -> list[dict]:
    today = today or date.today()
    window = _window(db, today)
    skip = set() if include_ignored else ignored(db)
    manual = _manual_rows(db)
    claimed = {e["id"] for g in manual.values() for e in g["entries"]}  # yours win over the guesses
    found = [_manual_item(g, today, window) for g in manual.values()]
    for (incoming, key), rows in _groups(db, claimed).items():
        if (key, "in" if incoming else "out") in skip:
            continue
        item = None
        dates = [date.fromisoformat(r["date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        cadence = _cadence(gaps) if len(rows) >= MIN_OCCURRENCES and len(gaps) >= MIN_OCCURRENCES - 1 else None
        pres = _presence(rows, window)
        if cadence:
            name, typical, per_month = cadence
            item = _item(key, incoming, rows, name, per_month, typical, "cadence", today, window,
                         pres if pres["seen_all"] else None)
        elif pres["ok"]:
            item = _item(key, incoming, rows, "monthly, irregular dates", 1.0, 30.44, "presence", today, window, pres)
        if item:
            found.append(item)
    found.sort(key=lambda f: -f["monthly_equivalent"])
    return found


def explain(db: Database, text: str, today: date | None = None) -> list[dict]:
    """Why a payee is or is not detected: the numbers behind the decision."""
    today = today or date.today()
    window = _window(db, today)
    needle = normalize(text)
    out = []
    for (incoming, key), rows in _groups(db).items():
        if needle not in key and needle not in normalize(rows[-1]["description"]):
            continue
        dates = [date.fromisoformat(r["date"]) for r in rows]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        cadence = _cadence(gaps) if len(gaps) >= MIN_OCCURRENCES - 1 else None
        pres = _presence(rows, window)
        amounts = [abs(r["amount"]) for r in rows]
        detected = any(i["key"] == key and i["direction"] == ("in" if incoming else "out")
                       for i in detect(db, today, include_ignored=True))
        out.append(dict(
            key=key, direction="in" if incoming else "out", occurrences=len(rows), detected=detected,
            hidden=(key, "in" if incoming else "out") in ignored(db),
            gaps_days=gaps[-12:], cadence=cadence[0] if cadence else None,
            amounts=dict(min=round(min(amounts), 2), max=round(max(amounts), 2), median=round(median(amounts), 2)),
            per_month=pres["counts"], presence_reasons=pres["reasons"], months_in_window=pres["window"]))
    return out


def summary(db: Database, today: date | None = None) -> dict:
    items = detect(db, today)
    active = [i for i in items if i["active"]]
    out = [i for i in active if i["direction"] == "out"]
    inc = [i for i in active if i["direction"] == "in"]
    hidden = [dict(key=k, direction=d) for k, d in sorted(ignored(db))]
    return dict(
        items=items,
        monthly_costs=round(sum(i["monthly_equivalent"] for i in out), 2),
        monthly_income=round(sum(i["monthly_equivalent"] for i in inc), 2),
        yearly_costs=round(12 * sum(i["monthly_equivalent"] for i in out), 2),
        upcoming=sorted((i for i in out if i["next_date"] >= (today or date.today()).isoformat()),
                        key=lambda i: i["next_date"])[:6],
        hidden=hidden)
