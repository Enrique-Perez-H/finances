"""Import a movements export from your bank (Santander's Excel file) to extend the history.

The bank connection only returns a limited window of history; an export reaches further back,
which helps recurring-cost detection and category rules. Everything is read locally with the
standard library only (an .xlsx file is a zip of XML). Entries the bank sync already holds are
recognised by account, amount and date (±3 days) and not added twice.
"""
import hashlib
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from .categorize import UNCATEGORIZED
from .db import Database

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
TOLERANCE_DAYS = 3
MARK = "-xl-"  # ids of imported entries contain this


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path) -> list[list[str]]:
    """First sheet as rows of text, empty cells kept so columns line up."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ValueError("that is not an Excel .xlsx file") from None
    with z:
        names = z.namelist()
        strings = []
        if "xl/sharedStrings.xml" in names:
            strings = ["".join(t.text or "" for t in si.iter(NS + "t"))
                       for si in ET.fromstring(z.read("xl/sharedStrings.xml"))]
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("the file has no sheet")
        rows = []
        for r in ET.fromstring(z.read(sheets[0])).iter(NS + "row"):
            cells = {}
            for k, c in enumerate(r.iter(NS + "c")):
                v, t = c.find(NS + "v"), c.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(NS + "t"))
                elif v is None:
                    val = ""
                else:
                    val = strings[int(v.text)] if t == "s" else v.text
                cells[_col(c.get("r")) if c.get("r") else k] = val
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        return rows


def parse_amount(text) -> float:
    s = re.sub(r"[^\d,.\-+]", "", str(text or ""))
    if not s or s in "+-":
        raise ValueError(f"cannot read the amount '{text}'")
    if "," in s:                       # Spanish: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def parse_date(text) -> str:
    s = str(text or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    if re.fullmatch(r"\d{5}(\.\d+)?", s):  # Excel serial date
        return (date(1899, 12, 30) + timedelta(days=int(float(s)))).isoformat()
    raise ValueError(f"cannot read the date '{text}'")


def parse_export(rows: list[list[str]]) -> list[dict]:
    """Movements as dicts (date, amount, description, balance). Header located by its column names."""
    head = None
    for i, r in enumerate(rows):
        names = [_norm(c) for c in r]
        if "importe" in names and "concepto" in names and any(n.startswith("fecha") for n in names):
            head = i
            break
    if head is None:
        raise ValueError("did not find the table (expected columns Fecha operación, Concepto, Importe)")
    names = [_norm(c) for c in rows[head]]
    pick = lambda *opts: next((names.index(o) for o in opts if o in names), None)  # noqa: E731
    c_date = pick("fecha operacion", "fecha")
    c_desc, c_amt, c_bal = pick("concepto"), pick("importe"), pick("saldo")
    out = []
    for r in rows[head + 1:]:
        get = lambda c: r[c] if c is not None and c < len(r) else ""  # noqa: E731
        if not str(get(c_amt)).strip() and not str(get(c_desc)).strip():
            continue
        out.append(dict(date=parse_date(get(c_date)), amount=parse_amount(get(c_amt)),
                        description=" ".join(str(get(c_desc)).split())[:140] or "Transaction",
                        balance=str(get(c_bal)).strip()))
    return out


def _bank_account(db: Database, account_id: str | None) -> str:
    if account_id:
        if not db.conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            raise ValueError("no such account")
        return account_id
    rows = [r[0] for r in db.conn.execute(
        "SELECT id FROM accounts WHERE kind != 'shared' AND provider NOT IN ('demo', 'trade_republic', 'manual')"
        " AND id NOT LIKE 'tricount%'")]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ValueError("no bank account yet: run a Santander sync first, then import")
    raise ValueError("several accounts found, pick one with --account: " + ", ".join(rows))


def _matches(existing: list[dict], row: dict, used: set) -> int | None:
    """Index of an unused bank-synced entry with the same amount and a date within the tolerance."""
    d = date.fromisoformat(row["date"])
    best = None
    for i, e in enumerate(existing):
        if i in used or round(e["amount"], 2) != round(row["amount"], 2):
            continue
        gap = abs((date.fromisoformat(e["date"]) - d).days)
        if gap <= TOLERANCE_DAYS and (best is None or gap < best[0]):
            best = (gap, i)
    return best[1] if best else None


def import_rows(db: Database, rows: list[dict], account_id: str | None = None) -> dict:
    account = _bank_account(db, account_id)
    existing = [dict(r) for r in db.conn.execute(
        f"SELECT id, date, amount FROM transactions WHERE account_id=? AND id NOT LIKE '%{MARK}%'", (account,))]
    used, added, matched, known = set(), 0, 0, 0
    seen: dict[str, int] = {}
    for row in sorted(rows, key=lambda r: r["date"]):
        key = f"{row['date']}|{row['amount']:.2f}|{row['description']}|{row['balance']}"
        seen[key] = seen.get(key, 0) + 1
        tx_id = f"{account}{MARK}" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]
        if db.conn.execute("SELECT 1 FROM transactions WHERE id=?", (tx_id,)).fetchone():
            known += 1
            continue
        hit = _matches(existing, row, used)
        if hit is not None:
            used.add(hit)
            matched += 1
            continue
        db.upsert_transaction(dict(id=tx_id, account_id=account, date=row["date"], amount=row["amount"],
                                   description=row["description"], category=UNCATEGORIZED))
        added += 1
    from .categorize import categorize_all
    categorize_all(db)
    dates = sorted(r["date"] for r in rows)
    return dict(account=account, read=len(rows), added=added, already_there=matched + known,
                first=dates[0] if dates else "", last=dates[-1] if dates else "")


def import_file(db: Database, path, account_id: str | None = None) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"file not found: {p}")
    return import_rows(db, parse_export(read_xlsx(p)), account_id)


def reconcile(db: Database) -> int:
    """After a bank sync: imported entries that the bank has since delivered itself are flagged as
    duplicates (reversible) so nothing counts twice. Returns how many were flagged."""
    flagged = 0
    for (account,) in db.conn.execute(
            f"SELECT DISTINCT account_id FROM transactions WHERE id LIKE '%{MARK}%'").fetchall():
        bank = [dict(r) for r in db.conn.execute(
            f"SELECT id, date, amount FROM transactions WHERE account_id=? AND id NOT LIKE '%{MARK}%'"
            " AND duplicate=0", (account,))]
        used = set()
        for r in db.conn.execute(
                f"SELECT id, date, amount FROM transactions WHERE account_id=? AND id LIKE '%{MARK}%'"
                " AND duplicate=0 ORDER BY date", (account,)).fetchall():
            hit = _matches(bank, dict(r), used)
            if hit is not None:
                used.add(hit)
                db.conn.execute("UPDATE transactions SET duplicate=1 WHERE id=?", (r["id"],))
                flagged += 1
    return flagged
