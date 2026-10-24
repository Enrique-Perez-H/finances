"""Spending categories: built-in keyword rules, your own rules, and a review queue.

Precedence for every transaction not set by hand: your rules, then built-in rules,
else 'uncategorized' (which the review queue asks you about). Manual per-entry choices
are never overwritten. Matching is on accent-free lowercase words.
"""
import re
import unicodedata
from collections import defaultdict

from .db import Database

UNCATEGORIZED = "uncategorized"
INTERNAL = "Internal transfer"  # moves between your own accounts: not spending, not income
INCOME_CATEGORIES = ["Salary", "Transfers received", "Interest & dividends", "Refunds",
                     "Other income"]
EXPENSE_CATEGORIES = [
    "Groceries", "Restaurants & bars", "Transport", "Fuel", "Housing", "Utilities", "Telecom",
    "Subscriptions", "Health", "Shopping", "Entertainment", "Travel", "Education", "Insurance",
    "Taxes", "Cash withdrawals", "Fees & interest", "Kids", "Transfers sent", "Other"]

# (pattern, category when money goes out, category when money comes in or None = Refunds)
DEFAULT_RULES = [
    ("mercadona", "Groceries", None), ("carrefour", "Groceries", None), ("lidl", "Groceries", None),
    ("aldi", "Groceries", None), ("dia", "Groceries", None), ("alcampo", "Groceries", None),
    ("eroski", "Groceries", None), ("consum", "Groceries", None), ("caprabo", "Groceries", None),
    ("bonpreu", "Groceries", None), ("ahorramas", "Groceries", None), ("supermercado", "Groceries", None),
    ("supermercados", "Groceries", None), ("fruteria", "Groceries", None),
    ("mcdonalds", "Restaurants & bars", None), ("mcdonald", "Restaurants & bars", None),
    ("burger king", "Restaurants & bars", None), ("telepizza", "Restaurants & bars", None),
    ("glovo", "Restaurants & bars", None), ("just eat", "Restaurants & bars", None),
    ("uber eats", "Restaurants & bars", None), ("starbucks", "Restaurants & bars", None),
    ("restaurante", "Restaurants & bars", None), ("cafeteria", "Restaurants & bars", None),
    ("pizzeria", "Restaurants & bars", None), ("kfc", "Restaurants & bars", None),
    ("renfe", "Transport", None), ("metro", "Transport", None), ("emt", "Transport", None),
    ("uber", "Transport", None), ("cabify", "Transport", None), ("bolt", "Transport", None),
    ("taxi", "Transport", None), ("blablacar", "Transport", None), ("parking", "Transport", None),
    ("alsa", "Transport", None), ("tmb", "Transport", None),
    ("repsol", "Fuel", None), ("cepsa", "Fuel", None), ("moeve", "Fuel", None), ("bp", "Fuel", None),
    ("shell", "Fuel", None), ("galp", "Fuel", None), ("gasolinera", "Fuel", None),
    ("iberdrola", "Utilities", None), ("endesa", "Utilities", None), ("naturgy", "Utilities", None),
    ("holaluz", "Utilities", None), ("octopus", "Utilities", None), ("agua", "Utilities", None),
    ("canal de isabel", "Utilities", None),
    ("movistar", "Telecom", None), ("vodafone", "Telecom", None), ("orange", "Telecom", None),
    ("masmovil", "Telecom", None), ("yoigo", "Telecom", None), ("digi", "Telecom", None),
    ("pepephone", "Telecom", None), ("jazztel", "Telecom", None),
    ("netflix", "Subscriptions", None), ("spotify", "Subscriptions", None),
    ("hbo", "Subscriptions", None), ("disney", "Subscriptions", None),
    ("amazon prime", "Subscriptions", None), ("prime video", "Subscriptions", None),
    ("youtube", "Subscriptions", None), ("icloud", "Subscriptions", None),
    ("google one", "Subscriptions", None), ("openai", "Subscriptions", None),
    ("anthropic", "Subscriptions", None), ("dazn", "Subscriptions", None),
    ("audible", "Subscriptions", None), ("patreon", "Subscriptions", None),
    ("gimnasio", "Health", None), ("basic fit", "Health", None), ("vivagym", "Health", None),
    ("farmacia", "Health", None), ("clinica", "Health", None), ("hospital", "Health", None),
    ("dentista", "Health", None), ("sanitas", "Health", None), ("adeslas", "Health", None),
    ("optica", "Health", None),
    ("amazon", "Shopping", None), ("zara", "Shopping", None), ("decathlon", "Shopping", None),
    ("ikea", "Shopping", None), ("el corte ingles", "Shopping", None), ("mediamarkt", "Shopping", None),
    ("fnac", "Shopping", None), ("leroy merlin", "Shopping", None), ("aliexpress", "Shopping", None),
    ("zalando", "Shopping", None), ("pccomponentes", "Shopping", None), ("primark", "Shopping", None),
    ("cine", "Entertainment", None), ("cinesa", "Entertainment", None),
    ("ticketmaster", "Entertainment", None), ("steam", "Entertainment", None),
    ("playstation", "Entertainment", None), ("nintendo", "Entertainment", None),
    ("booking", "Travel", None), ("airbnb", "Travel", None), ("ryanair", "Travel", None),
    ("vueling", "Travel", None), ("iberia", "Travel", None), ("easyjet", "Travel", None),
    ("hotel", "Travel", None),
    ("universidad", "Education", None), ("colegio", "Education", None), ("udemy", "Education", None),
    ("coursera", "Education", None),
    ("seguro", "Insurance", None), ("mapfre", "Insurance", None), ("mutua", "Insurance", None),
    ("axa", "Insurance", None), ("allianz", "Insurance", None), ("linea directa", "Insurance", None),
    ("hacienda", "Taxes", None), ("agencia tributaria", "Taxes", None), ("aeat", "Taxes", None),
    ("ayuntamiento", "Taxes", None), ("dgt", "Taxes", None), ("impuesto", "Taxes", None),
    ("alquiler", "Housing", None), ("hipoteca", "Housing", None), ("comunidad", "Housing", None),
    ("rent", "Housing", None),
    ("cajero", "Cash withdrawals", None), ("reintegro", "Cash withdrawals", None),
    ("atm", "Cash withdrawals", None),
    ("comision", "Fees & interest", "Refunds"), ("mantenimiento", "Fees & interest", "Refunds"),
    ("interes", "Fees & interest", "Interest & dividends"),
    ("intereses", "Fees & interest", "Interest & dividends"),
    ("guarderia", "Kids", None), ("kiabi", "Kids", None), ("toysrus", "Kids", None),
    ("bizum", "Transfers sent", "Transfers received"),
    ("transferencia", "Transfers sent", "Transfers received"),
    ("trade republic", INTERNAL, INTERNAL), ("traspaso", INTERNAL, INTERNAL),
    ("entre cuentas", INTERNAL, INTERNAL),
    ("nomina", "Salary", "Salary"), ("payroll", "Salary", "Salary"), ("salario", "Salary", "Salary"),
    ("salary", "Salary", "Salary"), ("haberes", "Salary", "Salary"),
    ("dividendo", "Interest & dividends", "Interest & dividends"),
    ("dividend", "Interest & dividends", "Interest & dividends"),
    ("devolucion", "Refunds", "Refunds"), ("reembolso", "Refunds", "Refunds"),
    ("refund", "Refunds", "Refunds"),
]

_NOISE = {"compra", "tarj", "tarjeta", "con", "pago", "movil", "en", "de", "la", "el", "los",
          "recibo", "a", "favor", "concepto", "ref", "referencia", "sepa", "comercio", "del",
          "por", "y", "al", "num", "no"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", text).split())


def merchant_key(text: str) -> str:
    """Short, stable label for grouping similar entries: first two meaningful words."""
    words = [w for w in normalize(text).split()
             if w not in _NOISE and not (any(c.isdigit() for c in w) and len(w) >= 3)
             and not set(w) <= {"x"}]
    return " ".join(words[:2])


def suggest_pattern(key: str) -> str:
    first = key.split(" ")[0] if key else ""
    return first if len(first) >= 5 else key


def _matcher(pattern: str):
    # Short patterns must match whole words ("dia" must not match "media").
    tail = r"(?![a-z0-9])" if len(pattern) <= 4 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(pattern) + tail)


def classify(text: str, amount: float, user_rules: list[tuple[str, str]]) -> tuple[str, str]:
    """Returns (category, source). Longest pattern wins within user rules and defaults."""
    norm = normalize(text)
    for pattern, category in sorted(user_rules, key=lambda r: -len(r[0])):
        if _matcher(pattern).search(norm):
            return category, "user"
    for pattern, cat_out, cat_in in sorted(DEFAULT_RULES, key=lambda r: -len(r[0])):
        if _matcher(pattern).search(norm):
            if amount >= 0:
                return (cat_in or "Refunds"), "default"
            return cat_out, "default"
    return UNCATEGORIZED, ""


def categorize_all(db: Database) -> int:
    """(Re)classify every entry not set by hand. Returns how many changed."""
    user_rules = [(r["pattern"], r["category"]) for r in db.conn.execute("SELECT * FROM rules")]
    changed = 0
    rows = db.conn.execute(
        "SELECT id, description, amount, category, cat_source FROM transactions"
        " WHERE cat_source != 'manual'").fetchall()
    for r in rows:
        category, source = classify(r["description"], r["amount"], user_rules)
        if (category, source) != (r["category"], r["cat_source"]):
            db.conn.execute("UPDATE transactions SET category=?, cat_source=? WHERE id=?",
                            (category, source, r["id"]))
            changed += 1
    return changed


def review_queue(db: Database, limit: int = 30) -> list[dict]:
    """Uncategorized entries grouped by merchant, biggest first, for you to decide."""
    groups = defaultdict(lambda: {"count": 0, "total": 0.0, "sample": "", "last": ""})
    for r in db.conn.execute(
            "SELECT description, amount, date FROM transactions WHERE category=? ORDER BY date",
            (UNCATEGORIZED,)):
        g = groups[merchant_key(r["description"]) or normalize(r["description"])]
        g["count"] += 1
        g["total"] += r["amount"]
        g["sample"] = r["description"]
        g["last"] = r["date"]
    out = [dict(key=k, pattern=suggest_pattern(k), count=g["count"], total=round(g["total"], 2),
                sample=g["sample"], last=g["last"]) for k, g in groups.items()]
    out.sort(key=lambda g: -abs(g["total"]))
    return out[:limit]


def add_rule(db: Database, pattern: str, category: str) -> int:
    pattern = normalize(pattern)
    if len(pattern) < 2 or not category.strip():
        raise ValueError("pattern and category are required")
    db.conn.execute("INSERT INTO rules(pattern, category) VALUES(?,?)"
                    " ON CONFLICT(pattern) DO UPDATE SET category=excluded.category",
                    (pattern, category.strip()))
    return categorize_all(db)


def delete_rule(db: Database, pattern: str) -> int:
    db.conn.execute("DELETE FROM rules WHERE pattern=?", (normalize(pattern),))
    return categorize_all(db)


def set_manual(db: Database, tx_id: str, category: str) -> None:
    cur = db.conn.execute("UPDATE transactions SET category=?, cat_source='manual' WHERE id=?",
                          (category.strip(), tx_id))
    if not cur.rowcount:
        raise ValueError("unknown transaction")


def all_categories(db: Database) -> list[str]:
    used = {r[0] for r in db.conn.execute("SELECT DISTINCT category FROM transactions")}
    used |= {r[0] for r in db.conn.execute("SELECT DISTINCT category FROM rules")}
    base = EXPENSE_CATEGORIES + INCOME_CATEGORIES + [INTERNAL]
    return base + sorted(c for c in used if c not in base and c != UNCATEGORIZED)
