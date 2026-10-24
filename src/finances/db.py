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
CREATE TABLE IF NOT EXISTS rules (
    pattern TEXT PRIMARY KEY,      -- normalized words, matched against transaction text
    category TEXT NOT NULL
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
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")}
        if "mcc" not in cols:  # merchant category code from the card network, when the bank gives one
            self.conn.execute("ALTER TABLE transactions ADD COLUMN mcc TEXT NOT NULL DEFAULT ''")
        if "cat_source" not in cols:  # '' auto/unset, 'default', 'user' (rule), 'manual' (one entry)
            self.conn.execute("ALTER TABLE transactions ADD COLUMN cat_source TEXT NOT NULL DEFAULT ''")

    def save(self) -> None:
        self.conn.commit()
        path = db_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(vault.encrypt(self.passphrase, self.conn.serialize()))
        tmp.chmod(0o600)
        tmp.replace(path)

    def upsert_account(self, a: dict) -> None:
        self.conn.execute(
            "INSERT INTO accounts(id,provider,name,kind,currency,balance,updated_at)"
            " VALUES(:id,:provider,:name,:kind,:currency,:balance,:updated_at)"
            " ON CONFLICT(id) DO UPDATE SET name=:name, balance=:balance,"
            " updated_at=:updated_at, currency=:currency, kind=:kind",
            a,
        )

    def upsert_transaction(self, t: dict) -> None:
        self.conn.execute(
            "INSERT INTO transactions(id,account_id,date,amount,description,category,mcc)"
            " VALUES(:id,:account_id,:date,:amount,:description,:category,:mcc)"
            " ON CONFLICT(id) DO UPDATE SET amount=:amount, description=:description, mcc=:mcc",
            {"mcc": "", **t},
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
