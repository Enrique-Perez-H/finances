"""Shared costs from a Tricount group (read-only).

Tricount has no public API. This uses the same private endpoints as its mobile app, reading
a group through its share link, so it can break whenever Tricount changes them. It never joins,
edits or deletes anything in your group. The link is kept only in the encrypted vault: anyone
holding it can read and edit the group.

Your bank entries stay the source of truth: they count in full unless you split them. Tricount
expenses are only a list to review. Nothing from Tricount counts until you choose, for each one:
* shared: your share of it is added to your spending (for what your partner paid, or cash...);
* only yours: all of it is added;
* already in my bank: left out, because the bank entry already counts it.
Settling-up payments are ignored: a transfer between you is categorised by your own rules.
"""
import json
import re
import urllib.error
import urllib.parse
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
DUPLICATE_DAYS_BEFORE, DUPLICATE_DAYS_AFTER = 5, 30  # where to look for a bank payment that already counts it
CATEGORY_HINTS = {
    "GROCERIES": "Groceries", "FOOD_AND_DRINK": "Restaurants & bars", "TRANSPORT": "Transport",
    "HEALTHCARE": "Health", "INSURANCE": "Insurance", "SHOPPING": "Shopping", "ENTERTAINMENT": "Leisure",
    "TRAVEL": "Travel", "RENT_AND_UTILITIES": "Utilities & telecom",
}


class TricountError(ConnectorError):
    pass


_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,64}")
_NOT_TOKENS = {"tricount", "share", "invite", "join", "download", "app", "group", "groups", "documents"}


def _shape(text: str) -> str:
    """What a pasted link looks like with the secret hidden: the host stays, each path piece shows
    only its first letter and its length."""
    host = re.search(r"https?://([^/\s?#]+)", text)
    pieces = [p for p in re.split(r"[/?#&=\s]+", re.sub(r"^.*?://[^/\s?#]+", "", text)) if p]
    shown = ", ".join(f"{p[0]}… ({len(p)} characters)" for p in pieces[-3:]) or "nothing after the address"
    return f"{host.group(1) if host else 'no address'} then {shown}"


def _looks_like_a_token(piece: str) -> bool:
    return bool(_TOKEN.fullmatch(piece)) and piece.lower() not in _NOT_TOKENS


def extract_token(link: str) -> str:
    """Find the group's token in whatever was pasted: the share link, a whole 'join my Tricount'
    message with the link inside, a link with a language or fragment part, or the bare token.
    The token is the last long piece of the address, e.g. tricount.com/XXXXXXXXXXXXXXXXX."""
    text = urllib.parse.unquote(str(link or "")).strip()
    candidates = []
    urls = re.findall(r"https?://[^\s<>\"']+", text)
    if not urls and re.search(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}/\S+", text):  # an address pasted without https://
        urls = ["https://" + m for m in re.findall(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}/[^\s<>\"']+", text)]
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        # the address path first; only if it holds nothing, the part after '#', then query values
        for pieces in ([p for p in parsed.path.split("/") if p],
                       [p for p in re.split(r"[/?&=]", parsed.fragment) if p],
                       [v for vs in urllib.parse.parse_qs(parsed.query).values() for v in vs]):
            found = [p for p in pieces if _looks_like_a_token(p)]
            if found:
                candidates += found
                break
    if not candidates and not urls:  # just the token, maybe with a few words around it
        words = [w.strip("/.,;:!?()") for w in re.split(r"\s+", text)]
        # a bare word must look like a code (digits or mixed case), not an ordinary word
        candidates = [w for w in words if _looks_like_a_token(w) and (re.search(r"\d", w) or (w != w.lower() and w != w.upper()))]
    if not candidates:
        raise TricountError("I could not find a Tricount token in that. It is the long code at the end of the link, "
                            f"like tricount.com/XXXXXXXXXXXXXXXXX. What I received looks like: {_shape(text)}")
    return candidates[-1]


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
        kinds = sorted({k for item in data.get("Response", []) if isinstance(item, dict) for k in item}) or ["nothing"]
        raise TricountError("Tricount did not return that group: check the link "
                            f"(it answered with: {', '.join(kinds)})")
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
            raise TricountError(f"Tricount does not know that link (HTTP {status}): check it, or whether the group was deleted")
        if status >= 400:
            raise TricountError(f"Tricount answered HTTP {status}")
        return parse_registry(data)


# --- your books: only what you choose ---------------------------------------------------------

CHOICES = ("", "shared", "mine", "skip")


def _virtual_id(uid: str) -> str:
    return f"tri-{uid}"


def _ensure_account(db: Database, now: str) -> None:
    db.upsert_account(dict(id=ACCOUNT_ID, provider="tricount", name="Added from Tricount", kind="shared",
                           currency="EUR", balance=0.0, updated_at=now))


def _drop_virtual(db: Database, uid: str) -> None:
    db.conn.execute("DELETE FROM transactions WHERE id=?", (_virtual_id(uid),))


def _put_virtual(db: Database, row: dict, now: str) -> None:
    """The entry that makes a chosen Tricount expense count: your share, or all of it."""
    _drop_virtual(db, row["uuid"])  # re-created so a changed date or amount follows the group
    amount = row["my_share"] if row["choice"] == "shared" else row["total"]
    if amount <= 0.004:
        return
    _ensure_account(db, now)
    hint = CATEGORY_HINTS.get(row["category"])
    db.upsert_transaction(dict(id=_virtual_id(row["uuid"]), account_id=ACCOUNT_ID, date=row["date"],
                               amount=-round(amount, 2), description=row["description"] or "Shared cost",
                               category=hint or "uncategorized", cat_source="connector" if hint else ""))


def store(db: Database, expenses: list[dict], now: str) -> dict:
    """Keep the list of Tricount expenses up to date. New ones start as 'to review': nothing about
    your figures changes until you choose. Choices already made follow edits made in the group."""
    stored = {r["uuid"]: dict(r) for r in db.conn.execute("SELECT * FROM tricount_expenses")}
    seen = set()
    for e in expenses:
        seen.add(e["uuid"])
        payer = "me" if e["payer_is_me"] else "other"
        choice = stored[e["uuid"]]["choice"] if e["uuid"] in stored else ""
        db.conn.execute(
            "INSERT INTO tricount_expenses(uuid, date, description, total, my_share, payer, category, choice)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET date=excluded.date,"
            " description=excluded.description, total=excluded.total, my_share=excluded.my_share,"
            " payer=excluded.payer, category=excluded.category",
            (e["uuid"], e["date"], e["description"], e["total"], e["my_share"], payer, e["category"], choice))
        if choice in ("shared", "mine"):
            _put_virtual(db, dict(uuid=e["uuid"], date=e["date"], description=e["description"], total=e["total"],
                                  my_share=e["my_share"], category=e["category"], choice=choice), now)
    for uid in stored:  # deleted in the group
        if uid not in seen:
            _drop_virtual(db, uid)
            db.conn.execute("DELETE FROM tricount_expenses WHERE uuid=?", (uid,))
    return summary(db)


def choose(db: Database, uids: list[str], choice: str, now: str | None = None) -> int:
    """Decide what an expense does to your figures: 'shared', 'mine', 'skip', or '' to take it back."""
    if choice not in CHOICES:
        raise ValueError("choose shared, mine, skip, or nothing to undo")
    if not uids or len(uids) > 500:
        raise ValueError("select between 1 and 500 expenses")
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for uid in uids:
        row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (str(uid),)).fetchone()
        if not row:
            raise ValueError("unknown Tricount expense")
        db.conn.execute("UPDATE tricount_expenses SET choice=? WHERE uuid=?", (choice, row["uuid"]))
        if choice in ("shared", "mine"):
            _put_virtual(db, dict(row, choice=choice), now)
        else:
            _drop_virtual(db, row["uuid"])
        n += 1
    return n


def in_bank(db: Database, row: dict) -> dict | None:
    """A bank payment that probably already counts this expense (same amount, nearby date), so you do
    not add it twice. Only a hint: nothing is matched or changed."""
    if row["payer"] != "me":
        return None  # your partner paid it: it cannot be in your bank
    d = date.fromisoformat(row["date"])
    lo, hi = d - timedelta(days=DUPLICATE_DAYS_BEFORE), d + timedelta(days=DUPLICATE_DAYS_AFTER)
    tolerance = max(0.01, 0.005 * row["total"])
    best = None
    for r in db.conn.execute(
            "SELECT id, date, amount, description FROM transactions WHERE amount < 0 AND account_id != ?"
            " AND date BETWEEN ? AND ? AND paired_with = ''", (ACCOUNT_ID, lo.isoformat(), hi.isoformat())):
        diff = abs(abs(r["amount"]) - row["total"])
        if diff <= tolerance:
            key = (abs((date.fromisoformat(r["date"]) - d).days), diff, r["id"])
            if best is None or key < best[0]:
                best = (key, dict(id=r["id"], date=r["date"], amount=r["amount"], description=r["description"][:60]))
    return best[1] if best else None


def summary(db: Database) -> dict:
    rows = [dict(r) for r in db.conn.execute(
        "SELECT uuid, date, description, total, my_share, payer, category, choice FROM tricount_expenses"
        " ORDER BY date DESC, uuid")]
    for r in rows:
        r["in_bank"] = in_bank(db, r) if r["choice"] in ("", "shared", "mine") else None
    counted = sum(r["my_share"] if r["choice"] == "shared" else r["total"] for r in rows if r["choice"] in ("shared", "mine"))
    return dict(expenses=rows, count=len(rows), pending=sum(r["choice"] == "" for r in rows),
                shared=sum(r["choice"] == "shared" for r in rows), mine=sum(r["choice"] == "mine" for r in rows),
                skipped=sum(r["choice"] == "skip" for r in rows), counted_total=round(counted, 2))


def sync(db: Database, state: dict, client: TricountClient | None = None, now: datetime | None = None,
         lock=None) -> dict:
    """Read the group and refresh the list. `state` is secrets["tricount"] (token, me, ...)."""
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
        out = store(db, result["expenses"], now.isoformat(timespec="seconds"))
    state.update(synced_at=now.isoformat(timespec="minutes"), balance=result["balance"], title=parsed["title"],
                 members=parsed["members"])
    return out


def disconnect(db: Database) -> None:
    """Forget the group: what you added from it disappears. Bank entries you split by hand stay split."""
    db.conn.execute("DELETE FROM transactions WHERE account_id=?", (ACCOUNT_ID,))
    db.conn.execute("DELETE FROM accounts WHERE id=?", (ACCOUNT_ID,))
    db.conn.execute("DELETE FROM tricount_expenses")


def set_share(db: Database, tx_ids: list[str], share: float) -> int:
    """Count only part of some bank entries as yours (shared by hand): 0.5 = half, 1 = all of it."""
    if not 0 <= share <= 1:
        raise ValueError("share must be between 0 and 1")
    if not tx_ids or len(tx_ids) > 500:
        raise ValueError("select between 1 and 500 entries")
    marks = ",".join("?" * len(tx_ids))
    cur = db.conn.execute(
        f"UPDATE transactions SET share=?, share_source=? WHERE id IN ({marks}) AND account_id != ?",
        (share, "" if share == 1 else "manual", *tx_ids, ACCOUNT_ID))
    return cur.rowcount
