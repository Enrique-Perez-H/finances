"""SQLite held in memory; persisted to disk only as an encrypted blob."""
import sqlite3

from . import vault
from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- cash | investment | savings
    currency TEXT NOT NULL DEFAULT 'EUR',
    balance REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,            -- ISO YYYY-MM-DD
    amount REAL NOT NULL,          -- negative = outflow
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'uncategorized'
);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
CREATE TABLE IF NOT EXISTS son_fund (
    id INTEGER PRIMARY KEY CHECK (id = 1),   -- a single fund
    name TEXT NOT NULL,
    account_id TEXT NOT NULL,      -- the account whose interest is shared
    start_date TEXT NOT NULL,      -- from when the starting amount is his
    initial REAL NOT NULL DEFAULT 0,
    monthly REAL NOT NULL DEFAULT 0,         -- planned extra deposits, projection only
    annual_rate REAL,              -- projection rate in %, NULL = estimate from real payments
    years INTEGER NOT NULL DEFAULT 18        -- how far to project
);
CREATE TABLE IF NOT EXISTS son_deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS balance_history (
    day TEXT NOT NULL,             -- ISO date of the sync that recorded it
    account_id TEXT NOT NULL,
    balance REAL NOT NULL,
    PRIMARY KEY (day, account_id)
);
CREATE TABLE IF NOT EXISTS account_settings (
    id TEXT PRIMARY KEY,           -- account id
    kind TEXT NOT NULL             -- your choice of type; survives later syncs
);
CREATE TABLE IF NOT EXISTS tricount_expenses (
    uuid TEXT PRIMARY KEY,         -- the expense in your Tricount group
    date TEXT NOT NULL,
    description TEXT NOT NULL,
    total REAL NOT NULL,           -- what was paid, all of it
    my_share REAL NOT NULL,        -- the part that is yours
    payer TEXT NOT NULL,           -- 'me' or 'other'
    ignored INTEGER NOT NULL DEFAULT 0,      -- you chose to leave it out
    tx_id TEXT NOT NULL DEFAULT '',          -- the bank entry it is matched to, or the virtual entry
    mode TEXT NOT NULL DEFAULT '',           -- 'linked' (bank entry) | 'virtual' | ''
    pinned INTEGER NOT NULL DEFAULT 0,       -- you confirmed the match (or "paid elsewhere"): never redone
    category TEXT NOT NULL DEFAULT ''        -- Tricount's own category
);
CREATE TABLE IF NOT EXISTS tricount_aliases (
    tri_key TEXT NOT NULL,         -- words in how you log the expense in Tricount, e.g. 'repostaje'
    bank_pattern TEXT NOT NULL,    -- words in how it shows in the bank, e.g. 'repsol'
    uses INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'learned'
    PRIMARY KEY (tri_key, bank_pattern)
);
CREATE TABLE IF NOT EXISTS recurring_manual (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    direction TEXT NOT NULL,       -- 'out' (a cost) or 'in'
    cadence TEXT NOT NULL DEFAULT 'monthly'
);
CREATE TABLE IF NOT EXISTS recurring_manual_entries (
    group_id INTEGER NOT NULL,
    tx_id TEXT NOT NULL,
    PRIMARY KEY (group_id, tx_id)
);
CREATE TABLE IF NOT EXISTS recurring_manual_patterns (
    group_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,         -- normalized words: later entries containing them join the group
    PRIMARY KEY (group_id, pattern)
);
CREATE TABLE IF NOT EXISTS recurring_ignored (
    key TEXT NOT NULL,             -- the payee key shown in the recurring list
    direction TEXT NOT NULL,       -- 'in' or 'out'
    PRIMARY KEY (key, direction)
);
CREATE TABLE IF NOT EXISTS category_aliases (
    src TEXT PRIMARY KEY,          -- a category name that was merged or renamed
    dst TEXT NOT NULL              -- where its entries live now
);
CREATE TABLE IF NOT EXISTS rules (
    pattern TEXT NOT NULL,         -- normalized words, matched against transaction text
    direction TEXT NOT NULL DEFAULT 'any',   -- 'out' (spending), 'in' (receipts) or 'any'
    category TEXT NOT NULL,
    PRIMARY KEY (pattern, direction)
);
CREATE TABLE IF NOT EXISTS fund_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    initial REAL NOT NULL DEFAULT 0,
    monthly REAL NOT NULL DEFAULT 0,
    annual_rate REAL NOT NULL DEFAULT 0.03,
    years INTEGER NOT NULL DEFAULT 18,
    contribution_growth REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 0,
    target REAL,
    link_account_id TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    value REAL NOT NULL,
    cost_basis REAL,
    PRIMARY KEY (account_id, symbol)
);
"""


class Database:
    def __init__(self, passphrase: str):
        self.passphrase = passphrase
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        path = db_path()
        if path.exists():
            self.conn.deserialize(vault.decrypt(passphrase, path.read_bytes()))
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        if "direction" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(rules)")}:
            self.conn.executescript("""
                CREATE TABLE rules_new (pattern TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'any',
                                        category TEXT NOT NULL, PRIMARY KEY (pattern, direction));
                INSERT INTO rules_new(pattern, direction, category) SELECT pattern, 'any', category FROM rules;
                DROP TABLE rules;
                ALTER TABLE rules_new RENAME TO rules;""")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")}
        tri = {r["name"] for r in self.conn.execute("PRAGMA table_info(tricount_expenses)")}
        if "pinned" not in tri:
            self.conn.execute("ALTER TABLE tricount_expenses ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "category" not in tri:
            self.conn.execute("ALTER TABLE tricount_expenses ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        if "share" not in cols:  # fraction of the amount that is yours (shared costs); 1 = all of it
            self.conn.execute("ALTER TABLE transactions ADD COLUMN share REAL NOT NULL DEFAULT 1.0")
        if "share_source" not in cols:  # '' | 'manual' | 'tricount'
            self.conn.execute("ALTER TABLE transactions ADD COLUMN share_source TEXT NOT NULL DEFAULT ''")
        if "paired_with" not in cols:  # id of the matching spend/reimbursement, '' if none
            self.conn.execute("ALTER TABLE transactions ADD COLUMN paired_with TEXT NOT NULL DEFAULT ''")
        if "pair_blocked" not in cols:  # 1 = you said this is not a reimbursement pair
            self.conn.execute("ALTER TABLE transactions ADD COLUMN pair_blocked INTEGER NOT NULL DEFAULT 0")
        if "mcc" not in cols:  # merchant category code from the card network, when the bank gives one
            self.conn.execute("ALTER TABLE transactions ADD COLUMN mcc TEXT NOT NULL DEFAULT ''")
        if "cat_source" not in cols:  # '' auto/unset, 'default', 'user' (rule), 'manual' (one entry)
            self.conn.execute("ALTER TABLE transactions ADD COLUMN cat_source TEXT NOT NULL DEFAULT ''")
        self.conn.executescript("""
            DROP VIEW IF EXISTS counted;
            CREATE VIEW counted AS
            SELECT id, account_id, date, amount * share AS amount, description, category, mcc, cat_source,
                   paired_with, pair_blocked, share, amount AS raw_amount
            FROM transactions;""")

    def save(self) -> None:
        self.conn.commit()
        path = db_path()
        from . import backup
        backup.rolling_copy()  # yesterday's state is kept before today's first write
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(vault.encrypt(self.passphrase, self.conn.serialize()))
        tmp.chmod(0o600)
        tmp.replace(path)

    def upsert_account(self, a: dict) -> None:
        self.conn.execute(
            "INSERT INTO accounts(id,provider,name,kind,currency,balance,updated_at)"
            " VALUES(:id,:provider,:name,:kind,:currency,:balance,:updated_at)"
            " ON CONFLICT(id) DO UPDATE SET name=:name, balance=:balance,"
            " updated_at=:updated_at, currency=:currency,"
            " kind=COALESCE((SELECT kind FROM account_settings WHERE id=:id), :kind)",
            a,
        )

    def upsert_transaction(self, t: dict) -> None:
        self.conn.execute(
            "INSERT INTO transactions(id,account_id,date,amount,description,category,mcc,cat_source)"
            " VALUES(:id,:account_id,:date,:amount,:description,:category,:mcc,:cat_source)"
            " ON CONFLICT(id) DO UPDATE SET amount=:amount, description=:description, mcc=:mcc",
            {"mcc": "", "cat_source": "", **t},
        )

    def replace_holdings(self, account_id: str, holdings: list[dict]) -> None:
        self.conn.execute("DELETE FROM holdings WHERE account_id=?", (account_id,))
        self.conn.executemany(
            "INSERT INTO holdings(account_id,symbol,quantity,value,cost_basis)"
            " VALUES(:account_id,:symbol,:quantity,:value,:cost_basis)",
            holdings,
        )

    def delete_provider(self, provider: str) -> int:
        """Remove every account of a provider with its transactions and holdings."""
        ids = [r[0] for r in self.conn.execute("SELECT id FROM accounts WHERE provider=?", (provider,))]
        for table in ("holdings", "transactions"):
            self.conn.executemany(f"DELETE FROM {table} WHERE account_id=?", [(i,) for i in ids])
        self.conn.execute("DELETE FROM accounts WHERE provider=?", (provider,))
        return len(ids)
