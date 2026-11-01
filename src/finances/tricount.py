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
import urllib.parse
import urllib.request
import uuid as uuidlib
from datetime import date, datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .categorize import GENERIC_PATTERNS, merchant_key, normalize, suggest_pattern
from .connectors.base import ConnectorError
from .db import Database

BASE_URL = "https://api.tricount.bunq.com"
# The private API only answers clients that look like the official app.
USER_AGENT = "com.bunq.tricount.android:RELEASE:7.0.7:3174:ANDROID:13:C"
ACCOUNT_ID = "tricount-shared"
MATCH_DAYS_BEFORE, MATCH_DAYS_AFTER = 5, 21  # bank payments usually post days after the expense is logged
ALIAS_DAYS_BEFORE, ALIAS_DAYS_AFTER = 10, 35  # wider when you told us how the payee appears in the bank
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


# --- applying it to your books -----------------------------------------------------------------

def _virtual_id(uid: str) -> str:
    return f"tri-{uid}"


def _ensure_account(db: Database, now: str) -> None:
    db.upsert_account(dict(id=ACCOUNT_ID, provider="tricount", name="Shared costs paid by others", kind="shared",
                           currency="EUR", balance=0.0, updated_at=now))


def _linked_ids(db: Database, ignoring: str = "") -> set[str]:
    return {r["tx_id"] for r in db.conn.execute(
        "SELECT tx_id FROM tricount_expenses WHERE mode='linked' AND uuid != ?", (ignoring,))}


_MONTHS = {"enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "setiembre",
           "octubre", "noviembre", "diciembre", "january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december", "gener", "febrer", "marc", "maig", "juny",
           "juliol", "setembre", "octubre", "novembre", "desembre"}


def tri_key(description: str) -> str:
    """How you call an expense in Tricount, without months and numbers: 'Fibra octubre 2026' -> 'fibra'."""
    words = [w for w in normalize(description).split() if not any(c.isdigit() for c in w) and w not in _MONTHS]
    return " ".join(words) or normalize(description)


def _has_words(text: str, words: str) -> bool:
    return bool(words) and f" {words} " in f" {text} "


def _alias_patterns(db: Database, description: str) -> list[str]:
    """The bank texts you associated with this kind of expense."""
    text = normalize(description)
    return [r["bank_pattern"] for r in db.conn.execute("SELECT tri_key, bank_pattern FROM tricount_aliases")
            if _has_words(text, r["tri_key"])]


def _window_pairs(db: Database, expenses: list[dict], taken: set[str]) -> list[tuple]:
    """Every (expense, bank payment) that could belong together: same amount (a cent of slack), the
    payment inside the date window, or inside the wider one when its text matches a hint you gave.
    Sorted best first: nearest date, then closest amount."""
    pairs = []
    for e in expenses:
        d = date.fromisoformat(e["date"])
        patterns = _alias_patterns(db, e.get("description", ""))
        lo, hi = d - timedelta(days=MATCH_DAYS_BEFORE), d + timedelta(days=MATCH_DAYS_AFTER)
        a_lo, a_hi = d - timedelta(days=ALIAS_DAYS_BEFORE), d + timedelta(days=ALIAS_DAYS_AFTER)
        tolerance = max(0.01, 0.005 * e["total"])
        wide = (a_lo, a_hi) if patterns else (lo, hi)
        for r in db.conn.execute(
                "SELECT id, date, amount, description FROM transactions WHERE amount < 0 AND account_id != ?"
                " AND date BETWEEN ? AND ? AND paired_with = ''", (ACCOUNT_ID, wide[0].isoformat(), wide[1].isoformat())):
            diff = abs(abs(r["amount"]) - e["total"])
            if diff > tolerance or r["id"] in taken:
                continue
            rd = date.fromisoformat(r["date"])
            if not (lo <= rd <= hi):  # outside the usual window: only for a payee you named
                text = normalize(r["description"])
                if not any(_has_words(text, p) for p in patterns):
                    continue
            pairs.append((abs((rd - d).days), diff, r["id"], e["uuid"]))
    pairs.sort()
    return pairs


def assign_matches(db: Database, expenses: list[dict], taken: set[str]) -> dict[str, str]:
    """One bank payment per expense, decided together: the closest pairs are settled first, so two
    similar monthly expenses cannot steal each other's payment. Returns {expense uuid: bank entry id}."""
    chosen, used_tx = {}, set()
    for _, _, tx_id, uid in _window_pairs(db, expenses, taken):
        if uid not in chosen and tx_id not in used_tx:
            chosen[uid] = tx_id
            used_tx.add(tx_id)
    return chosen


def find_bank_match(db: Database, day: str, total: float, taken: set[str]) -> str | None:
    """The best bank payment for one expense, or None."""
    return assign_matches(db, [dict(uuid="?", date=day, total=total)], taken).get("?")


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
    seen, records, pending = set(), {}, []
    for e in expenses:  # pass 1: everything that does not need a match decided with the others
        seen.add(e["uuid"])
        row = stored.get(e["uuid"])
        mine = e["my_share"]
        ignored = bool(row["ignored"]) if row else False
        pinned = bool(row["pinned"]) if row else False
        if pinned and row["mode"] == "linked" and not db.conn.execute(
                "SELECT 1 FROM transactions WHERE id=?", (row["tx_id"],)).fetchone():
            pinned = False  # the bank entry is gone: start again
        changed = bool(row) and not pinned and (abs(row["total"] - e["total"]) > 0.004 or row["date"] != e["date"])
        mode, tx_id = (row["mode"], row["tx_id"]) if row and not changed else ("", "")
        if row and changed:  # the group edited it: forget the old matching and start again
            if row["mode"] == "linked":
                _clear_share(db, row["tx_id"])
            _drop_virtual(db, e["uuid"])
        if ignored or mine <= 0.004:
            if mode == "linked":
                _clear_share(db, tx_id)
            _drop_virtual(db, e["uuid"])
            mode, tx_id = "", ""
        elif pinned and mode == "linked":  # you confirmed it: only the share may change
            _set_share(db, tx_id, mine / e["total"])
        elif pinned and mode == "virtual":  # you said it was paid elsewhere
            _put_virtual(db, e)
        elif e["payer_is_me"]:
            if mode == "linked" and db.conn.execute("SELECT 1 FROM transactions WHERE id=?", (tx_id,)).fetchone():
                _set_share(db, tx_id, mine / e["total"])
            else:
                pending.append(e)
                mode, tx_id = "", ""
        else:
            _put_virtual(db, e)
            mode, tx_id = "virtual", _virtual_id(e["uuid"])
        records[e["uuid"]] = dict(e=e, ignored=ignored, pinned=pinned and mode != "", mode=mode, tx_id=tx_id)
    # pass 2: match what you paid to bank payments, all together
    taken = {r["tx_id"] for r in records.values() if r["mode"] == "linked"}
    matches = assign_matches(db, pending, taken | {r["tx_id"] for u, r in stored.items()
                                                     if r["mode"] == "linked" and u not in seen})
    for e in pending:
        rec = records[e["uuid"]]
        if e["uuid"] in matches:
            _drop_virtual(db, e["uuid"])
            _set_share(db, matches[e["uuid"]], e["my_share"] / e["total"])
            rec.update(mode="linked", tx_id=matches[e["uuid"]])
        else:
            _put_virtual(db, e)  # paid from somewhere not synced: your share still counts
            rec.update(mode="virtual", tx_id=_virtual_id(e["uuid"]))
    for uid, rec in records.items():
        e = rec["e"]
        db.conn.execute(
            "INSERT INTO tricount_expenses(uuid, date, description, total, my_share, payer, ignored, tx_id, mode, pinned, category)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET date=excluded.date,"
            " description=excluded.description, total=excluded.total, my_share=excluded.my_share,"
            " payer=excluded.payer, tx_id=excluded.tx_id, mode=excluded.mode, pinned=excluded.pinned,"
            " category=excluded.category",
            (uid, e["date"], e["description"], e["total"], e["my_share"], "me" if e["payer_is_me"] else "other",
             int(rec["ignored"]), rec["tx_id"], rec["mode"], int(rec["pinned"]), e["category"]))
    for uid, row in stored.items():  # deleted in the group
        if uid not in seen:
            if row["mode"] == "linked":
                _clear_share(db, row["tx_id"])
            _drop_virtual(db, uid)
            db.conn.execute("DELETE FROM tricount_expenses WHERE uuid=?", (uid,))
    return summary(db)


def rematch(db: Database) -> int:
    """Bank entries that arrived after the expense was logged: turn virtual entries into matches."""
    rows = db.conn.execute("SELECT * FROM tricount_expenses WHERE mode='virtual' AND payer='me' AND ignored=0"
                           " AND pinned=0").fetchall()
    matches = assign_matches(db, [dict(uuid=r["uuid"], date=r["date"], total=r["total"],
                                       description=r["description"]) for r in rows], _linked_ids(db))
    by_uuid = {r["uuid"]: r for r in rows}
    for uid, tx_id in matches.items():
        r = by_uuid[uid]
        _drop_virtual(db, uid)
        _set_share(db, tx_id, r["my_share"] / r["total"])
        db.conn.execute("UPDATE tricount_expenses SET mode='linked', tx_id=? WHERE uuid=?", (tx_id, uid))
    return len(matches)


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
        confirmed_elsewhere=sum(r["mode"] == "virtual" and r["pinned"] == 1 and r["payer"] == "me" for r in rows),
        unmatched_mine=[r for r in rows if r["mode"] == "virtual" and r["payer"] == "me" and not r["pinned"]
                        and not r["ignored"]],
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


# --- confirming matches by hand ---------------------------------------------------------------

CANDIDATE_DAYS = 45
CANDIDATE_MAX_AMOUNT_DIFF = 0.30  # the bank amount may differ from the group's total by up to 30%


def candidates(db: Database, uid: str, limit: int = 4) -> list[dict]:
    """Bank payments that could be this expense, best first: close amount, close date, shared words."""
    from .categorize import normalize
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    day = date.fromisoformat(row["date"])
    taken = _linked_ids(db, uid)
    words = set(normalize(row["description"]).split())
    patterns = _alias_patterns(db, row["description"])
    out = []
    for r in db.conn.execute(
            "SELECT b.id, b.date, b.amount, b.description, b.category, a.name AS account FROM transactions b"
            " JOIN accounts a ON a.id = b.account_id WHERE b.amount < 0 AND b.account_id != ? AND b.paired_with = ''"
            " AND b.date BETWEEN ? AND ?",
            (ACCOUNT_ID, (day - timedelta(days=CANDIDATE_DAYS)).isoformat(), (day + timedelta(days=CANDIDATE_DAYS)).isoformat())):
        if r["id"] in taken:
            continue
        diff = abs(r["amount"]) - row["total"]
        rel = abs(diff) / row["total"] if row["total"] else 1.0
        if rel > CANDIDATE_MAX_AMOUNT_DIFF:
            continue
        days = (date.fromisoformat(r["date"]) - day).days
        shared_words = words & set(normalize(r["description"]).split())
        hinted = any(_has_words(normalize(r["description"]), p) for p in patterns)
        score = rel * 20 + abs(days) / 15 - (0.15 if shared_words else 0) - (0.5 if hinted else 0)  # amount, date, then text
        how = "same amount" if abs(diff) < 0.005 else f"{'more' if diff > 0 else 'less'} by {abs(diff):.2f}"
        when = "same day" if days == 0 else f"{abs(days)} day{'s' if abs(days) != 1 else ''} {'later' if days > 0 else 'earlier'}"
        out.append(dict(id=r["id"], date=r["date"], amount=r["amount"], description=r["description"][:70],
                        category=r["category"], account=r["account"], score=round(score, 3),
                        reason=f"{how}, {when}" + (", matches your hint" if hinted else ", similar text" if shared_words else "")))
    out.sort(key=lambda c: (c["score"], c["date"], c["id"]))
    return out[:limit]


def link(db: Database, uid: str, tx_id: str) -> None:
    """You confirm this bank payment is the expense. Its share is applied and the match is never redone."""
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    if row["payer"] != "me":
        raise ValueError("only expenses you paid can be matched to your bank")
    tx = db.conn.execute("SELECT id, amount, account_id FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx or tx["amount"] >= 0 or tx["account_id"] == ACCOUNT_ID:
        raise ValueError("choose one of your bank payments")
    if tx_id in _linked_ids(db, uid):
        raise ValueError("that payment is already matched to another expense")
    if row["mode"] == "linked":
        _clear_share(db, row["tx_id"])
    _drop_virtual(db, uid)
    _set_share(db, tx_id, (row["my_share"] / row["total"]) if row["total"] else 1.0)
    db.conn.execute("UPDATE tricount_expenses SET mode='linked', tx_id=?, pinned=1, ignored=0 WHERE uuid=?", (tx_id, uid))
    bank = db.conn.execute("SELECT description FROM transactions WHERE id=?", (tx_id,)).fetchone()
    learn(db, row["description"], bank["description"])


def confirm_elsewhere(db: Database, uid: str) -> None:
    """There is no bank entry (paid in cash, another account): keep counting your share, stop asking."""
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    if row["mode"] == "linked":
        _clear_share(db, row["tx_id"])
    _put_virtual(db, dict(uuid=uid, date=row["date"], description=row["description"], my_share=row["my_share"],
                          category=row["category"]))
    db.conn.execute("UPDATE tricount_expenses SET mode='virtual', tx_id=?, pinned=1, ignored=0 WHERE uuid=?",
                    (_virtual_id(uid), uid))


def unlink(db: Database, uid: str) -> None:
    """Undo a confirmation: back to a virtual entry that can be matched again."""
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    if row["mode"] == "linked":
        _clear_share(db, row["tx_id"])
    _put_virtual(db, dict(uuid=uid, date=row["date"], description=row["description"], my_share=row["my_share"],
                          category=row["category"]))
    db.conn.execute("UPDATE tricount_expenses SET mode='virtual', tx_id=?, pinned=0 WHERE uuid=?", (_virtual_id(uid), uid))


def search_payments(db: Database, uid: str, q: str = "", days: int = 90, limit: int = 15) -> list[dict]:
    """Look through your bank payments by hand when the suggestions are not the right ones: any amount,
    around the expense's date, optionally containing some words (accents and capitals ignored)."""
    from .categorize import normalize
    row = db.conn.execute("SELECT * FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()
    if not row:
        raise ValueError("unknown Tricount expense")
    day = date.fromisoformat(row["date"])
    days = max(1, min(int(days), 365))
    needle = normalize(q or "")
    taken = _linked_ids(db, uid)
    out = []
    for r in db.conn.execute(
            "SELECT b.id, b.date, b.amount, b.description, b.category, a.name AS account FROM transactions b"
            " JOIN accounts a ON a.id = b.account_id WHERE b.amount < 0 AND b.account_id != ? AND b.paired_with = ''"
            " AND b.date BETWEEN ? AND ?",
            (ACCOUNT_ID, (day - timedelta(days=days)).isoformat(), (day + timedelta(days=days)).isoformat())):
        if r["id"] in taken or (needle and needle not in normalize(r["description"])):
            continue
        diff = abs(r["amount"]) - row["total"]
        gap = (date.fromisoformat(r["date"]) - day).days
        how = "same amount" if abs(diff) < 0.005 else f"{'more' if diff > 0 else 'less'} by {abs(diff):.2f}"
        when = "same day" if gap == 0 else f"{abs(gap)} day{'s' if abs(gap) != 1 else ''} {'later' if gap > 0 else 'earlier'}"
        out.append(dict(id=r["id"], date=r["date"], amount=r["amount"], description=r["description"][:70],
                        category=r["category"], account=r["account"], reason=f"{how}, {when}",
                        _key=(abs(abs(diff)) if not needle else abs(gap), abs(gap), r["id"])))
    out.sort(key=lambda c: c["_key"])
    for c in out:
        del c["_key"]
    return out[:limit]


# --- hints: what your Tricount words mean in the bank ------------------------------------------

def _bank_pattern(bank_description: str) -> str:
    return suggest_pattern(merchant_key(bank_description) or normalize(bank_description), bank_description)


def _usable(tri: str, bank: str) -> bool:
    return (len(tri) >= 3 and len(bank) >= 3 and bank not in GENERIC_PATTERNS and tri not in GENERIC_PATTERNS)


def learn(db: Database, tri_description: str, bank_description: str) -> None:
    """You confirmed a match: remember how this expense shows up in the bank, for next time."""
    tri, bank = tri_key(tri_description), _bank_pattern(bank_description)
    if _usable(tri, bank):
        db.conn.execute("INSERT INTO tricount_aliases(tri_key, bank_pattern, uses, source) VALUES(?,?,1,'learned')"
                        " ON CONFLICT(tri_key, bank_pattern) DO UPDATE SET uses = uses + 1", (tri, bank))


def add_alias(db: Database, tri: str, bank: str) -> None:
    tri, bank = normalize(tri), normalize(bank)
    if not _usable(tri, bank):
        raise ValueError("give at least 3 letters on each side, and not a word like 'transferencia'")
    db.conn.execute("INSERT INTO tricount_aliases(tri_key, bank_pattern, uses, source) VALUES(?,?,0,'manual')"
                    " ON CONFLICT(tri_key, bank_pattern) DO UPDATE SET source='manual'", (tri, bank))


def delete_alias(db: Database, tri: str, bank: str) -> None:
    db.conn.execute("DELETE FROM tricount_aliases WHERE tri_key=? AND bank_pattern=?", (normalize(tri), normalize(bank)))


def aliases(db: Database) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT tri_key, bank_pattern, uses, source FROM tricount_aliases ORDER BY tri_key, bank_pattern")]


def suggest_aliases(db: Database, min_uses: int = 2, limit: int = 20) -> list[dict]:
    """Pairs seen in at least two of your matched expenses (e.g. 'mercadona' paid at 'mercadona')
    that are not hints yet. Repetition makes a coincidence of amounts unlikely."""
    known = {(a["tri_key"], a["bank_pattern"]) for a in aliases(db)}
    counts = {}
    for r in db.conn.execute(
            "SELECT e.description AS tri, t.description AS bank FROM tricount_expenses e"
            " JOIN transactions t ON t.id = e.tx_id WHERE e.mode = 'linked'"):
        pair = (tri_key(r["tri"]), _bank_pattern(r["bank"]))
        if _usable(*pair) and pair not in known:
            counts[pair] = counts.get(pair, 0) + 1
    out = [dict(tri_key=t, bank_pattern=b, seen=n) for (t, b), n in counts.items() if n >= min_uses]
    out.sort(key=lambda s: (-s["seen"], s["tri_key"]))
    return out[:limit]
