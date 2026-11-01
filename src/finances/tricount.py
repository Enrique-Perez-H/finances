"""Shared costs from a Tricount group (read-only).

Tricount has no public API. This uses the same private endpoints as its mobile app, reading
a group through its share link, so it can break whenever Tricount changes them. It never joins,
edits or deletes anything in your group. The link is kept only in the encrypted vault: anyone
holding it can read and edit the group.

What it does with your own books:
* only YOUR share of every expense counts as your spending, whoever paid;
* an expense you paid is matched to the bank entry (same amount, nearby date) and that entry
  then counts at your share, e.g. half of the daycare;
* an expense your partner paid appears as a virtual entry for your share, e.g. half of the gas;
* settling-up payments are ignored: a transfer between you is categorised by your own rules.
"""
import json
import re
import urllib.error
import urllib.request
import uuid as uuidlib
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .connectors.base import ConnectorError
from .db import Database

BASE_URL = "https://api.tricount.bunq.com"
# The private API only answers clients that look like the official app.
USER_AGENT = "com.bunq.tricount.android:RELEASE:7.0.7:3174:ANDROID:13:C"
ACCOUNT_ID = "tricount-shared"
MATCH_DAYS_BEFORE, MATCH_DAYS_AFTER = 3, 10
CATEGORY_HINTS = {
    "GROCERIES": "Groceries", "FOOD_AND_DRINK": "Restaurants & bars", "TRANSPORT": "Transport",
    "HEALTHCARE": "Health", "INSURANCE": "Insurance", "SHOPPING": "Shopping", "ENTERTAINMENT": "Leisure",
    "TRAVEL": "Travel", "RENT_AND_UTILITIES": "Utilities & telecom",
}


class TricountError(ConnectorError):
    pass


def extract_token(link: str) -> str:
    """Accept the whole share link or just the token (it starts with 't')."""
    text = str(link or "").strip().split("?")[0].rstrip("/")
    token = text.rsplit("/", 1)[-1]
    if not re.fullmatch(r"t[A-Za-z0-9]{6,40}", token):
        raise TricountError("that does not look like a Tricount share link (it ends in something like tAbC123xyz)")
    return token


def _first(d: dict | None) -> dict:
    """The API wraps objects as {"SomeTypeName": {...}}: take the inside whatever the name."""
    if isinstance(d, dict):
        for value in d.values():
            if isinstance(value, dict):
                return value
    return {}


def parse_registry(data: dict) -> dict:
    registry = None
    for item in data.get("Response", []):
        if isinstance(item, dict) and "Registry" in item:
            registry = item["Registry"]
    if registry is None:
        raise TricountError("Tricount did not return that group: check the link")
    members = []
    for m in registry.get("memberships", []):
        inner = _first(m)
        name = (inner.get("alias") or {}).get("display_name") or inner.get("uuid", "")
        if inner.get("uuid"):
            members.append(dict(uuid=inner["uuid"], name=str(name)))
    entries = []
    for entry in registry.get("all_registry_entry", []):
        e = entry.get("RegistryEntry") if isinstance(entry, dict) else None
        if not e:
            continue
        payer = e.get("membership_uuid_owner") or _first(e.get("membership_owned")).get("uuid", "")
        shares = {}
        for a in e.get("allocations", []):
            who = a.get("membership_uuid") or _first(a.get("membership")).get("uuid", "")
            try:
                shares[who] = shares.get(who, 0.0) + abs(float((a.get("amount") or {}).get("value", 0)))
            except (TypeError, ValueError):
                pass
        try:
            total = abs(float((e.get("amount") or {}).get("value", 0)))
        except (TypeError, ValueError):
            continue
        entries.append(dict(uuid=str(e.get("uuid", "")), description=str(e.get("description", ""))[:140],
                            date=str(e.get("date", ""))[:10], total=total, payer=payer, shares=shares,
                            type=e.get("type_transaction", "NORMAL"), status=e.get("status", "ACTIVE"),
                            category=e.get("category") or ""))
    return dict(title=str(registry.get("title", "")), currency=str(registry.get("currency", "")),
                members=members, entries=entries)


def evaluate(parsed: dict, me: str) -> dict:
    """Your share of every expense, and who owes whom."""
    expenses, net = [], 0.0
    for e in parsed["entries"]:
        if e["status"] == "INACTIVE" or not e["uuid"]:
            continue
        if e["type"] == "BALANCE":  # someone paid someone back
            receivers = [w for w, amount in e["shares"].items() if amount > 0 and w != e["payer"]]
            if e["payer"] == me:
                net += e["total"]
            elif me in receivers:
                net -= e["total"]
            continue
        if e["type"] != "NORMAL":
            continue  # shared income is not modelled
        mine = e["shares"].get(me, 0.0)
        net += (e["total"] if e["payer"] == me else 0.0) - mine
        expenses.append(dict(e, payer_is_me=e["payer"] == me, my_share=round(mine, 2)))
    return dict(expenses=expenses, balance=round(net, 2))


class TricountClient:
    def __init__(self, state: dict, http=None):
        self.state = state  # secrets["tricount"]: keeps the app identity between runs
        self._http = http or self._urllib_http
        self.user_id = None
        self.headers = {}

    @staticmethod
    def _urllib_http(method, url, headers, body=None):
        req = urllib.request.Request(url, method=method, headers={**headers, "Content-Type": "application/json"},
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError) as e:
            raise TricountError(f"could not reach Tricount ({type(e).__name__})") from None

    def _app(self) -> dict:
        app = self.state.get("app")
        if not app:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.PKCS1).decode()
            app = self.state["app"] = {"app_id": str(uuidlib.uuid4()), "public_key_pem": pem}
        return app

    def authenticate(self) -> None:
        app = self._app()
        base = {"User-Agent": USER_AGENT, "app-id": app["app_id"], "X-Bunq-Client-Request-Id": str(uuidlib.uuid4())}
        status, data = self._http("POST", f"{BASE_URL}/v1/session-registry-installation", base,
                                  {"app_installation_uuid": app["app_id"], "client_public_key": app["public_key_pem"],
                                   "device_description": "Android"})
        token, user = None, None
        for item in data.get("Response", []) if status < 400 else []:
            if "Token" in item:
                token = item["Token"].get("token")
            if "UserPerson" in item:
                user = item["UserPerson"].get("id")
        if not token or user is None:
            raise TricountError(f"Tricount refused the connection (HTTP {status}); its private interface may have changed")
        self.user_id = user
        self.headers = {**base, "X-Bunq-Client-Authentication": token}

    def fetch(self, token: str) -> dict:
        if self.user_id is None:
            self.authenticate()
        status, data = self._http("GET", f"{BASE_URL}/v1/user/{self.user_id}/registry?public_identifier_token={token}",
                                  self.headers)
        if status == 404 or (status < 400 and not data.get("Response")):
            raise TricountError("Tricount does not know that link: check it, or whether the group was deleted")
        if status >= 400:
            raise TricountError(f"Tricount answered HTTP {status}")
        return parse_registry(data)


# --- applying it to your books -----------------------------------------------------------------

def _virtual_id(uid: str) -> str:
    return f"tri-{uid}"


def _ensure_account(db: Database, now: str) -> None:
    db.upsert_account(dict(id=ACCOUNT_ID, provider="tricount", name="Shared costs paid by others", kind="shared",
                           currency="EUR", balance=0.0, updated_at=now))


def _linked_ids(db: Database, ignoring: str = "") -> set[str]:
    return {r["tx_id"] for r in db.conn.execute(
        "SELECT tx_id FROM tricount_expenses WHERE mode='linked' AND uuid != ?", (ignoring,))}


def find_bank_match(db: Database, day: str, total: float, taken: set[str]) -> str | None:
    d = date.fromisoformat(day)
    lo, hi = (d - timedelta(days=MATCH_DAYS_BEFORE)).isoformat(), (d + timedelta(days=MATCH_DAYS_AFTER)).isoformat()
    tolerance = max(0.01, 0.005 * total)
    best = None
    for r in db.conn.execute(
            "SELECT id, date, amount FROM transactions WHERE amount < 0 AND account_id != ? AND date BETWEEN ? AND ?"
            " AND paired_with = ''", (ACCOUNT_ID, lo, hi)):
        diff = abs(abs(r["amount"]) - total)
        if diff <= tolerance and r["id"] not in taken:
            key = (abs((date.fromisoformat(r["date"]) - d).days), diff, r["id"])
            if best is None or key < best[0]:
                best = (key, r["id"])
    return best[1] if best else None


def _set_share(db: Database, tx_id: str, share: float) -> None:
    db.conn.execute("UPDATE transactions SET share=?, share_source='tricount' WHERE id=?", (round(share, 6), tx_id))


def _clear_share(db: Database, tx_id: str) -> None:
    db.conn.execute("UPDATE transactions SET share=1.0, share_source='' WHERE id=? AND share_source='tricount'", (tx_id,))


def _drop_virtual(db: Database, uid: str) -> None:
    db.conn.execute("DELETE FROM transactions WHERE id=?", (_virtual_id(uid),))


def _put_virtual(db: Database, e: dict) -> None:
    hint = CATEGORY_HINTS.get(e["category"])
    db.upsert_transaction(dict(id=_virtual_id(e["uuid"]), account_id=ACCOUNT_ID, date=e["date"],
                               amount=-e["my_share"], description=e["description"] or "Shared cost",
                               category=hint or "uncategorized", cat_source="connector" if hint else ""))


def apply(db: Database, expenses: list[dict], now: str) -> dict:
    """Bring the books in line with the group. Safe to run again and again."""
    _ensure_account(db, now)
    stored = {r["uuid"]: dict(r) for r in db.conn.execute("SELECT * FROM tricount_expenses")}
    seen = set()
    for e in expenses:
        seen.add(e["uuid"])
        row = stored.get(e["uuid"])
        mine = e["my_share"]
        payer = "me" if e["payer_is_me"] else "other"
        changed = bool(row) and (abs(row["total"] - e["total"]) > 0.004 or row["date"] != e["date"])
        mode, tx_id = (row["mode"], row["tx_id"]) if row and not changed else ("", "")
        if row and changed:  # the group edited it: forget the old matching and start again
            if row["mode"] == "linked":
                _clear_share(db, row["tx_id"])
            _drop_virtual(db, e["uuid"])
        ignored = bool(row["ignored"]) if row else False
        if ignored or mine <= 0.004:
            if mode == "linked":
                _clear_share(db, tx_id)
            _drop_virtual(db, e["uuid"])
            mode, tx_id = "", ""
        elif e["payer_is_me"]:
            if mode == "linked" and db.conn.execute("SELECT 1 FROM transactions WHERE id=?", (tx_id,)).fetchone():
                _set_share(db, tx_id, mine / e["total"])
            else:
                match = find_bank_match(db, e["date"], e["total"], _linked_ids(db, e["uuid"]))
                if match:
                    _drop_virtual(db, e["uuid"])
                    _set_share(db, match, mine / e["total"])
                    mode, tx_id = "linked", match
                else:
                    _put_virtual(db, e)  # paid from somewhere not synced: your share still counts
                    mode, tx_id = "virtual", _virtual_id(e["uuid"])
        else:
            _put_virtual(db, e)
            mode, tx_id = "virtual", _virtual_id(e["uuid"])
        db.conn.execute(
            "INSERT INTO tricount_expenses(uuid, date, description, total, my_share, payer, ignored, tx_id, mode)"
            " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET date=excluded.date,"
            " description=excluded.description, total=excluded.total, my_share=excluded.my_share,"
            " payer=excluded.payer, tx_id=excluded.tx_id, mode=excluded.mode",
            (e["uuid"], e["date"], e["description"], e["total"], mine, payer, int(ignored), tx_id, mode))
    for uid, row in stored.items():  # deleted in the group
        if uid not in seen:
            if row["mode"] == "linked":
                _clear_share(db, row["tx_id"])
            _drop_virtual(db, uid)
            db.conn.execute("DELETE FROM tricount_expenses WHERE uuid=?", (uid,))
    return summary(db)


def rematch(db: Database) -> int:
    """A bank entry that arrived after the expense was logged: turn the virtual entry into a match."""
    n = 0
    for r in db.conn.execute("SELECT * FROM tricount_expenses WHERE mode='virtual' AND payer='me' AND ignored=0").fetchall():
        match = find_bank_match(db, r["date"], r["total"], _linked_ids(db, r["uuid"]))
        if match:
            _drop_virtual(db, r["uuid"])
            _set_share(db, match, r["my_share"] / r["total"])
            db.conn.execute("UPDATE tricount_expenses SET mode='linked', tx_id=? WHERE uuid=?", (match, r["uuid"]))
            n += 1
    return n


def set_ignored(db: Database, uid: str, ignored: bool) -> None:
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    db.conn.execute("UPDATE tricount_expenses SET ignored=? WHERE uuid=?", (int(ignored), uid))
    if ignored:
        if row["mode"] == "linked":
            _clear_share(db, row["tx_id"])
        _drop_virtual(db, uid)
        db.conn.execute("UPDATE tricount_expenses SET mode='', tx_id='' WHERE uuid=?", (uid,))


def summary(db: Database) -> dict:
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM tricount_expenses ORDER BY date DESC, uuid")]
    return dict(
        expenses=rows, count=len(rows),
        linked=sum(r["mode"] == "linked" for r in rows), virtual=sum(r["mode"] == "virtual" for r in rows),
        unmatched_mine=[r for r in rows if r["mode"] == "virtual" and r["payer"] == "me"],
        ignored=sum(bool(r["ignored"]) for r in rows),
        my_total=round(sum(r["my_share"] for r in rows if not r["ignored"]), 2))


def sync(db: Database, state: dict, client: TricountClient | None = None, now: datetime | None = None,
         lock=None) -> dict:
    """Read the group and update the books. `state` is secrets["tricount"] (token, me, ...)."""
    import contextlib
    if not state.get("token") or not state.get("me"):
        raise TricountError("connect a Tricount first (Shared tab)")
    now = now or datetime.now(timezone.utc)
    client = client or TricountClient(state)
    parsed = client.fetch(state["token"])  # network first, outside the lock
    if parsed["currency"] and parsed["currency"] != "EUR":
        raise TricountError(f"this group is in {parsed['currency']}; only EUR groups are supported for now")
    if state["me"] not in {m["uuid"] for m in parsed["members"]}:
        raise TricountError("you are no longer a member of that group: choose who you are again")
    result = evaluate(parsed, state["me"])
    with (lock or contextlib.nullcontext()):
        out = apply(db, result["expenses"], now.isoformat(timespec="seconds"))
    state.update(synced_at=now.isoformat(timespec="minutes"), balance=result["balance"], title=parsed["title"],
                 members=parsed["members"])
    return out


def disconnect(db: Database) -> None:
    """Forget the group: every entry goes back to counting in full, virtual entries disappear."""
    db.conn.execute("UPDATE transactions SET share=1.0, share_source='' WHERE share_source='tricount'")
    db.conn.execute("DELETE FROM transactions WHERE account_id=?", (ACCOUNT_ID,))
    db.conn.execute("DELETE FROM accounts WHERE id=?", (ACCOUNT_ID,))
    db.conn.execute("DELETE FROM tricount_expenses")


def set_share(db: Database, tx_ids: list[str], share: float) -> int:
    """Count only part of some bank entries as yours (shared by hand, no Tricount needed)."""
    if not 0 <= share <= 1:
        raise ValueError("share must be between 0 and 1")
    if not tx_ids or len(tx_ids) > 500:
        raise ValueError("select between 1 and 500 entries")
    marks = ",".join("?" * len(tx_ids))
    if db.conn.execute(f"SELECT 1 FROM transactions WHERE id IN ({marks}) AND share_source='tricount'", tx_ids).fetchone():
        raise ValueError("some of those are matched to a Tricount expense: change it in Tricount")
    cur = db.conn.execute(
        f"UPDATE transactions SET share=?, share_source=? WHERE id IN ({marks}) AND account_id != ?",
        (share, "" if share == 1 else "manual", *tx_ids, ACCOUNT_ID))
    return cur.rowcount
