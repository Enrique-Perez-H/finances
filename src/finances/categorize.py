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
INCOME_CATEGORIES = ["Salary", "Pension & benefits", "Transfers received", "Interest & dividends",
                     "Refunds", "Other income"]
EXPENSE_CATEGORIES = [
    "Groceries", "Restaurants & bars", "Transport", "Housing", "Home & furniture",
    "Utilities & telecom", "Subscriptions", "Health", "Shopping", "Leisure", "Travel",
    "Education", "Insurance", "Taxes & fees", "Cash withdrawals", "Kids", "Donations", "Other"]

# Names used by earlier versions -> current names. Seeded into the category_aliases table,
# which also records merges you make, so old names never come back.
LEGACY_ALIASES = {
    "Fuel": "Transport", "Telecom": "Utilities & telecom", "Utilities": "Utilities & telecom",
    "Entertainment": "Leisure", "Sports & leisure": "Leisure", "Transfers sent": "Other",
    "Fees & interest": "Taxes & fees", "Taxes": "Taxes & fees",
}

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
    ("repsol", "Transport", None), ("cepsa", "Transport", None), ("moeve", "Transport", None), ("bp", "Transport", None),
    ("shell", "Transport", None), ("galp", "Transport", None), ("gasolinera", "Transport", None),
    ("iberdrola", "Utilities & telecom", None), ("endesa", "Utilities & telecom", None), ("naturgy", "Utilities & telecom", None),
    ("holaluz", "Utilities & telecom", None), ("octopus", "Utilities & telecom", None), ("agua", "Utilities & telecom", None),
    ("canal de isabel", "Utilities & telecom", None),
    ("movistar", "Utilities & telecom", None), ("vodafone", "Utilities & telecom", None), ("orange", "Utilities & telecom", None),
    ("masmovil", "Utilities & telecom", None), ("yoigo", "Utilities & telecom", None), ("digi", "Utilities & telecom", None),
    ("pepephone", "Utilities & telecom", None), ("jazztel", "Utilities & telecom", None),
    ("netflix", "Subscriptions", None), ("spotify", "Subscriptions", None),
    ("hbo", "Subscriptions", None), ("disney", "Subscriptions", None),
    ("amazon prime", "Subscriptions", None), ("prime video", "Subscriptions", None),
    ("youtube", "Subscriptions", None), ("icloud", "Subscriptions", None),
    ("google one", "Subscriptions", None), ("openai", "Subscriptions", None),
    ("anthropic", "Subscriptions", None), ("dazn", "Subscriptions", None),
    ("audible", "Subscriptions", None), ("patreon", "Subscriptions", None),
    ("farmacia", "Health", None), ("clinica", "Health", None), ("hospital", "Health", None),
    ("dentista", "Health", None), ("sanitas", "Health", None), ("adeslas", "Health", None),
    ("optica", "Health", None),
    ("amazon", "Shopping", None), ("zara", "Shopping", None), ("decathlon", "Shopping", None),
    ("ikea", "Home & furniture", None), ("el corte ingles", "Shopping", None), ("mediamarkt", "Shopping", None),
    ("fnac", "Shopping", None), ("leroy merlin", "Home & furniture", None), ("aliexpress", "Shopping", None),
    ("zalando", "Shopping", None), ("pccomponentes", "Shopping", None), ("primark", "Shopping", None),
    ("cine", "Leisure", None), ("cinesa", "Leisure", None),
    ("ticketmaster", "Leisure", None), ("steam", "Leisure", None),
    ("playstation", "Leisure", None), ("nintendo", "Leisure", None),
    ("booking", "Travel", None), ("airbnb", "Travel", None), ("ryanair", "Travel", None),
    ("vueling", "Travel", None), ("iberia", "Travel", None), ("easyjet", "Travel", None),
    ("hotel", "Travel", None),
    ("universidad", "Education", None), ("colegio", "Education", None), ("udemy", "Education", None),
    ("coursera", "Education", None),
    ("seguro", "Insurance", None), ("mapfre", "Insurance", None), ("mutua", "Insurance", None),
    ("axa", "Insurance", None), ("allianz", "Insurance", None), ("linea directa", "Insurance", None),
    ("hacienda", "Taxes & fees", None), ("agencia tributaria", "Taxes & fees", None), ("aeat", "Taxes & fees", None),
    ("ayuntamiento", "Taxes & fees", None), ("dgt", "Taxes & fees", None), ("impuesto", "Taxes & fees", None),
    ("alquiler", "Housing", None), ("hipoteca", "Housing", None), ("comunidad", "Housing", None),
    ("rent", "Housing", None),
    ("cajero", "Cash withdrawals", None), ("reintegro", "Cash withdrawals", None),
    ("atm", "Cash withdrawals", None),
    ("comision", "Taxes & fees", "Refunds"), ("mantenimiento", "Taxes & fees", "Refunds"),
    ("interes", "Taxes & fees", "Interest & dividends"),
    ("intereses", "Taxes & fees", "Interest & dividends"),
    ("guarderia", "Kids", None), ("kiabi", "Kids", None), ("toysrus", "Kids", None),
    # local shops and common Spanish business words
    ("guissona", "Groceries", None), ("bonarea", "Groceries", None), ("panaderia", "Groceries", None),
    ("carniceria", "Groceries", None), ("pescaderia", "Groceries", None), ("charcuteria", "Groceries", None),
    ("ultramarinos", "Groceries", None), ("pasteleria", "Groceries", None), ("horno", "Groceries", None),
    ("formatges", "Groceries", None), ("queseria", "Groceries", None), ("herbolario", "Groceries", None),
    ("tasca", "Restaurants & bars", None), ("taberna", "Restaurants & bars", None),
    ("bar", "Restaurants & bars", None), ("cerveceria", "Restaurants & bars", None),
    ("asador", "Restaurants & bars", None), ("noodles", "Restaurants & bars", None),
    ("sushi", "Restaurants & bars", None), ("kebab", "Restaurants & bars", None),
    ("burger", "Restaurants & bars", None), ("meson", "Restaurants & bars", None),
    ("restaurant", "Restaurants & bars", None), ("tapas", "Restaurants & bars", None),
    ("bodega", "Restaurants & bars", None), ("heladeria", "Restaurants & bars", None),
    ("pub", "Restaurants & bars", None), ("carnivore", "Restaurants & bars", None),
    ("calzados", "Shopping", None), ("zapateria", "Shopping", None), ("colchones", "Home & furniture", None),
    ("joyeria", "Shopping", None), ("boutique", "Shopping", None), ("women secret", "Shopping", None),
    ("women secrets", "Shopping", None), ("ferreteria", "Home & furniture", None), ("papeleria", "Shopping", None),
    ("libreria", "Shopping", None), ("muebles", "Home & furniture", None), ("cc", "Shopping", None),
    ("centro comercial", "Shopping", None), ("estanco", "Other", None),
    ("salud", "Health", None), ("fisioterapia", "Health", None), ("podologo", "Health", None),
    ("veterinario", "Health", None), ("dental", "Health", None), ("parafarmacia", "Health", None),
    ("piscina", "Leisure", None), ("polideportivo", "Leisure", None),
    ("padel", "Leisure", None), ("gimnasio", "Leisure", None),
    ("basic fit", "Leisure", None), ("vivagym", "Leisure", None),
    ("intermon", "Donations", None), ("oxfam", "Donations", None), ("unicef", "Donations", None),
    ("cruz roja", "Donations", None), ("greenpeace", "Donations", None), ("acnur", "Donations", None),
    ("medicos sin fronteras", "Donations", None), ("save the children", "Donations", None),
    ("fundacion", "Donations", None),
    ("conforama", "Home & furniture", None), ("bricolaje", "Home & furniture", None),
    ("bauhaus", "Home & furniture", None), ("maisons du monde", "Home & furniture", None),
    ("electrodomesticos", "Home & furniture", None), ("bricomart", "Home & furniture", None),
    ("brico depot", "Home & furniture", None), ("cocinas", "Home & furniture", None),
    ("tgss", "Taxes & fees", None), ("seguridad social", "Taxes & fees", None),
    ("cotizacion", "Taxes & fees", None), ("tesoreria", "Taxes & fees", "Pension & benefits"),
    ("pension", "Other", "Pension & benefits"), ("jubilacion", "Other", "Pension & benefits"),
    ("bizum", "Other", "Transfers received"),
    ("trade republic", INTERNAL, INTERNAL), ("traderepublic", INTERNAL, INTERNAL),
    ("trbkdebb", INTERNAL, INTERNAL), ("traspaso", INTERNAL, INTERNAL),
    ("entre cuentas", INTERNAL, INTERNAL),
    ("nomina", "Salary", "Salary"), ("payroll", "Salary", "Salary"), ("salario", "Salary", "Salary"),
    ("salary", "Salary", "Salary"), ("haberes", "Salary", "Salary"),
    ("dividendo", "Interest & dividends", "Interest & dividends"),
    ("dividend", "Interest & dividends", "Interest & dividends"),
    ("devolucion", "Refunds", "Refunds"), ("reembolso", "Refunds", "Refunds"),
    ("refund", "Refunds", "Refunds"),
]

# Merchant category codes (ISO 18245) -> category, used when no keyword rule matches.
def _mcc_ranges():
    table = {}
    def add(category, *codes):
        for c in codes:
            for code in (range(c[0], c[1] + 1) if isinstance(c, tuple) else [c]):
                table[str(code)] = category
    add("Groceries", 5411, 5422, 5441, 5451, 5462, 5499, 5300)
    add("Restaurants & bars", 5811, 5812, 5813, 5814)
    add("Transport", 5541, 5542, 5983)
    add("Transport", 4111, 4112, 4121, 4131, 4784, 7523, 7512, 4789)
    add("Health", 5122, 5912, 5975, 5976, 7298, 7997, (8011, 8099))
    add("Shopping", 5200, 5251, 5311, 5331, 5399, 5611, 5621, 5631, 5641, 5651, 5661, 5691, 5699,
        5712, 5732, 5734, 5735, 5921, 5931, 5941, 5942, 5945, 5947, 5948, 5970, 5977, 5992, 5993,
        5994, 5995, 5999, 5310, 5719, 5722, 5733)
    add("Leisure", 7829, 7832, 7841, 7911, 7922, 7929, 7932, 7933, 7941, 7991, 7992, 7993,
        7994, 7996, 7998, 7999, 5816)
    add("Travel", 4411, 4511, 4722, 7011, 3000, (3000, 3299), (3500, 3999), 4723, 7032, 7033)
    add("Utilities & telecom", 4812, 4814, 4816, 4899)
    add("Utilities & telecom", 4900, 4821)
    add("Insurance", 6300, 6381, 6399)
    add("Taxes & fees", 9311, 9399, 9222, 9211)
    add("Cash withdrawals", 6010, 6011)
    add("Education", 8211, 8220, 8241, 8244, 8249, 8299, 8351)
    add("Subscriptions", 5815, 5817, 5818, 5968, 5192)
    add("Housing", 6513, 1520, 1711, 1731, 1740, 1750, 1761, 1771, 7349)
    add("Taxes & fees", 6012, 6051, 6540)
    return table


MCC_CATEGORIES = _mcc_ranges()

_NOISE = {"compra", "tarj", "tarjeta", "con", "pago", "movil", "en", "de", "la", "el", "los",
          "recibo", "a", "favor", "concepto", "ref", "referencia", "sepa", "comercio", "del",
          "por", "y", "al", "num", "no"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", text).split())


# Descriptions that name no merchant: a rule on them would label every such payment alike.
GENERIC_KEYS = {"transaccion contactless", "transaccion", "pago contactless", "compra contactless",
                "contactless", "operacion tarjeta", "pago tarjeta", "compra tarjeta"}


def _looks_random(word: str) -> bool:
    """Reference codes like 'bbpzcbg': long, almost no vowels."""
    return len(word) >= 5 and sum(c in "aeiou" for c in word) / len(word) < 0.2


def merchant_key(text: str) -> str:
    """Short, stable label for grouping similar entries: first two meaningful words."""
    words = [w for w in normalize(text).split()
             if w not in _NOISE and not (any(c.isdigit() for c in w) and len(w) >= 3)
             and not set(w) <= {"x"}]
    kept = [w for w in words if not _looks_random(w)] or words
    return " ".join(kept[:2])


def is_generic(key: str) -> bool:
    return key in GENERIC_KEYS


def suggest_pattern(key: str) -> str:
    first = key.split(" ")[0] if key else ""
    return first if len(first) >= 5 else key


def _matcher(pattern: str):
    # Short patterns must match whole words ("dia" must not match "media").
    tail = r"(?![a-z0-9])" if len(pattern) <= 4 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(pattern) + tail)


def classify(text: str, amount: float, user_rules: list[tuple[str, str]], mcc: str = "",
             aliases: dict | None = None) -> tuple[str, str]:
    """Returns (category, source). Order: your rules, built-in keywords, merchant category code."""
    category, source = _classify(text, amount, user_rules, mcc)
    return _canon(category, aliases), source


def _canon(category: str, aliases: dict | None = None) -> str:
    merged = {**LEGACY_ALIASES, **(aliases or {})}
    for _ in range(5):  # a merge may chain (A into B, later B into C)
        if category not in merged:
            break
        category = merged[category]
    return category


def _classify(text, amount, user_rules, mcc):
    norm = normalize(text)
    for pattern, category in sorted(user_rules, key=lambda r: -len(r[0])):
        if _matcher(pattern).search(norm):
            return category, "user"
    for pattern, cat_out, cat_in in sorted(DEFAULT_RULES, key=lambda r: -len(r[0])):
        if _matcher(pattern).search(norm):
            if amount >= 0:
                return (cat_in or "Refunds"), "default"
            return cat_out, "default"
    category = MCC_CATEGORIES.get(str(mcc or "").strip())
    if category:
        return ("Refunds" if amount >= 0 else category), "mcc"
    return UNCATEGORIZED, ""


def _fold(name: str) -> str:
    return name.strip().casefold().rstrip("s")


def aliases(db: Database) -> dict:
    """Your merges, plus automatic ones for near-duplicates of a built-in name
    (different capitals or a trailing 's': 'other' -> 'Other', 'Insurances' -> 'Insurance')."""
    builtin = EXPENSE_CATEGORIES + INCOME_CATEGORIES + [INTERNAL]
    folded = {_fold(c): c for c in builtin}
    used = {r[0] for r in db.conn.execute("SELECT DISTINCT category FROM transactions")}
    used |= {r[0] for r in db.conn.execute("SELECT DISTINCT category FROM rules")}
    auto = {c: folded[_fold(c)] for c in used
            if c not in builtin and c != UNCATEGORIZED and _fold(c) in folded}
    return {**auto, **{r["src"]: r["dst"] for r in db.conn.execute("SELECT src, dst FROM category_aliases")}}


def apply_aliases(db: Database) -> int:
    """Rename old category names everywhere, including entries and rules you set yourself."""
    n = 0
    for src, dst in {**LEGACY_ALIASES, **aliases(db)}.items():
        dst = _canon(dst, aliases(db))
        n += db.conn.execute("UPDATE transactions SET category=? WHERE category=?", (dst, src)).rowcount
        db.conn.execute("UPDATE rules SET category=? WHERE category=?", (dst, src))
    return n


def merge_category(db: Database, src: str, dst: str) -> int:
    """Move everything in `src` to `dst` and keep it that way (also renames)."""
    src, dst = src.strip(), dst.strip()
    if not src or not dst or len(dst) > 40:
        raise ValueError("both names are required (new name up to 40 characters)")
    if UNCATEGORIZED in (src, dst) or src == dst:
        raise ValueError("choose two different categories; 'uncategorized' cannot be merged")
    db.conn.execute("UPDATE category_aliases SET dst=? WHERE dst=?", (dst, src))  # flatten chains
    db.conn.execute("INSERT INTO category_aliases(src, dst) VALUES(?,?)"
                    " ON CONFLICT(src) DO UPDATE SET dst=excluded.dst", (src, dst))
    db.conn.execute("DELETE FROM category_aliases WHERE src=dst")
    moved = apply_aliases(db)
    categorize_all(db)
    return moved


def categorize_all(db: Database) -> int:
    """(Re)classify every entry not set by hand or by its connector. Returns how many changed."""
    apply_aliases(db)
    user_rules = [(r["pattern"], r["category"]) for r in db.conn.execute("SELECT * FROM rules")]
    alias_map = aliases(db)
    changed = 0
    rows = db.conn.execute(
        "SELECT id, description, amount, category, cat_source, mcc FROM transactions"
        " WHERE cat_source NOT IN ('manual', 'connector')").fetchall()
    for r in rows:
        category, source = classify(r["description"], r["amount"], user_rules, r["mcc"], alias_map)
        if (category, source) != (r["category"], r["cat_source"]):
            db.conn.execute("UPDATE transactions SET category=?, cat_source=? WHERE id=?",
                            (category, source, r["id"]))
            changed += 1
    from . import pairs  # after classification: pairing looks at the final categories
    return changed + pairs.apply(db)


def review_queue(db: Database, limit: int = 30) -> list[dict]:
    """Uncategorized entries grouped by merchant, biggest first, for you to decide."""
    groups = defaultdict(lambda: {"count": 0, "total": 0.0, "sample": "", "last": ""})
    for r in db.conn.execute(
            "SELECT description, amount, date FROM transactions WHERE category=? AND paired_with='' ORDER BY date",
            (UNCATEGORIZED,)):
        g = groups[merchant_key(r["description"]) or normalize(r["description"])]
        g["count"] += 1
        g["total"] += r["amount"]
        g["sample"] = r["description"]
        g["last"] = r["date"]
    out = [dict(key=k, pattern=suggest_pattern(k), count=g["count"], total=round(g["total"], 2),
                sample=g["sample"], last=g["last"], generic=False)
           for k, g in groups.items() if not is_generic(k)]
    out.sort(key=lambda g: -abs(g["total"]))
    return out[:limit]


def generic_entries(db: Database, limit: int = 15) -> dict:
    """Uncategorized entries that name no merchant (e.g. 'contactless'): one by one."""
    rows = [r for r in db.conn.execute(
        "SELECT id, date, amount, description FROM transactions WHERE category=? AND paired_with=''"
        " ORDER BY ABS(amount) DESC", (UNCATEGORIZED,))
        if is_generic(merchant_key(r["description"]) or normalize(r["description"]))]
    return dict(total=len(rows), entries=[dict(r) for r in rows[:limit]])


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
    merged_away = set(LEGACY_ALIASES) | set(aliases(db))
    return base + sorted(c for c in used if c not in base and c != UNCATEGORIZED
                         and c not in merged_away)
