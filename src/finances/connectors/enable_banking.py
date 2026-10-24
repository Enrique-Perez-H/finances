"""Santander (or any PSD2 bank) via Enable Banking: account information only, read-only.

Enable Banking's "restricted production" mode is free for accounts you link to your own
application. Secrets used:
  eb_app_id        application id (uuid) shown in the Enable Banking control panel
  eb_private_key   contents of the <app id>.pem file downloaded when registering
  eb_redirect_url  optional, default http://localhost:8765/callback (must be whitelisted)
  eb_bank          optional bank name to look for, default "Santander" (country ES)
The approved bank consent ("session") is stored encrypted in `eb_session`.

Flow: no valid session -> NeedsInput with the bank's approval link. The user approves
in the bank app/site, lands on the redirect URL and gives back the `code` (the dashboard
does this automatically for /callback, or the user pastes the address). Consent lasts
at most what the bank allows (often 90-180 days), then the flow repeats.

Only GET calls on accounts, balances and transactions are made, plus the two calls that
create the read-only consent (POST /auth, POST /sessions). Nothing can move money.
"""
import base64
import hashlib
import json
import secrets as _secrets
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .base import Connector, ConnectorError, NeedsInput, Skipped, Snapshot

API = "https://api.enablebanking.com"
DEFAULT_REDIRECT = "http://localhost:8765/callback"
MAX_CONSENT = timedelta(days=180)
FIRST_SYNC_DAYS, LATER_SYNC_DAYS = 730, 60
# Banks may cap background (PSU-absent) access at ~4 requests per 24 h (PSD2). A request that
# carries the PSU headers says "the user is present", which banks do not cap this way.
UNATTENDED_DAILY_BUDGET = 4
ATTENDED_MIN_INTERVAL = timedelta(minutes=10)
BALANCE_PREFERENCE = ["CLBD", "ITBD", "OPBD", "CLAV", "ITAV", "XPCD"]


class EnableBankingError(ConnectorError):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"Enable Banking HTTP {status}" + (f": {message}" if message else ""))
        self.status = status


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: str, private_key_pem: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    iat = int(now.timestamp())
    payload = {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
               "iat": iat, "exp": iat + 3600}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


def extract_code(text: str, expected_state: str | None = None) -> str:
    """Accept either the bare code or the whole address the bank redirected to."""
    text = text.strip()
    if "code=" in text or "error=" in text:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query or text.lstrip("?"))
        if "error" in query:
            raise ValueError(f"The bank reported an error: {query['error'][0]}")
        if expected_state and query.get("state", [expected_state])[0] != expected_state:
            raise ValueError("The approval did not come from this login attempt; start again.")
        if "code" in query:
            return query["code"][0]
        raise ValueError("No code found in that address.")
    return text


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def map_transaction(tx: dict, account_id: str, seen: Counter) -> dict | None:
    if tx.get("status", "BOOK") != "BOOK":  # pending items change; wait until booked
        return None
    amount = Decimal(str(tx["transaction_amount"]["amount"]))
    outgoing = tx.get("credit_debit_indicator") == "DBIT"
    amount = -abs(amount) if outgoing else abs(amount)
    party = (tx.get("creditor") if outgoing else tx.get("debtor")) or {}
    remit = tx.get("remittance_information") or []
    remit = " ".join(remit) if isinstance(remit, list) else str(remit)
    code = (tx.get("bank_transaction_code") or {}).get("description")
    desc = " – ".join(p for p in (party.get("name"), remit) if p) or code or "Transaction"
    date = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
    if not date:
        return None
    ref = tx.get("transaction_id") or tx.get("entry_reference")
    if not ref:
        fingerprint = f"{date}|{amount}|{desc}"
        seen[fingerprint] += 1  # identical same-day payments must not collapse into one
        ref = hashlib.sha1(f"{fingerprint}|{seen[fingerprint]}".encode()).hexdigest()[:16]
    return dict(id=f"{account_id}-{ref}", account_id=account_id, date=date[:10],
                amount=float(amount), description=desc[:140], category="uncategorized",
                mcc=str(tx.get("merchant_category_code") or ""))


class EnableBankingConnector(Connector):
    name = "enable_banking"

    def __init__(self, secrets: dict, http=None, now=None):
        super().__init__(secrets)
        self._http = http or self._urllib_http
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._state = None

    # --- HTTP -------------------------------------------------------------
    def _urllib_http(self, method, path, body=None, params=None, headers=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        token = make_jwt(self.secrets["eb_app_id"], self.secrets["eb_private_key"], self._now())
        req = urllib.request.Request(
            url, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("message", "")
            except Exception:
                detail = ""
            raise EnableBankingError(e.code, str(detail)[:200]) from None

    # --- consent / session ------------------------------------------------
    def _session_alive(self) -> bool:
        s = self.secrets.get("eb_session")
        return bool(s and _parse_time(s["valid_until"]) > self._now())

    def _find_bank(self) -> dict:
        wanted = self.secrets.get("eb_bank", "Santander").lower()
        banks = self._http("GET", "/aspsps", params={"country": "ES", "psu_type": "personal"})
        found = banks.get("aspsps", [])
        matches = [b for b in found if b["name"].lower() == wanted]  # exact name wins
        matches = matches or [b for b in found if wanted in b["name"].lower()]
        if len(matches) != 1:
            names = ", ".join(sorted(b["name"] for b in matches)) or "none found"
            raise RuntimeError(f"Expected one bank matching '{wanted}', got: {names}. "
                               "Set the exact name with `finances secret eb_bank`.")
        return matches[0]

    def _start_authorization(self):
        bank = self._find_bank()
        validity = min(MAX_CONSENT, timedelta(seconds=bank.get("maximum_consent_validity", 0))
                       or MAX_CONSENT) - timedelta(minutes=5)
        self._state = _secrets.token_urlsafe(16)
        resp = self._http("POST", "/auth", body={
            "access": {"valid_until": (self._now() + validity).isoformat(),
                       "balances": True, "transactions": True},
            "aspsp": {"name": bank["name"], "country": bank["country"]},
            "state": self._state,
            "redirect_url": self.secrets.get("eb_redirect_url", DEFAULT_REDIRECT),
            "psu_type": "personal",
        })
        self._bank_name = bank["name"]
        self._required_headers = bank.get("required_psu_headers") or []
        raise NeedsInput(f"Approve access in {bank['name']}, then return here", url=resp["url"],
                         field="code")

    def _create_session(self, user_input: str):
        code = extract_code(user_input, self._state)
        resp = self._http("POST", "/sessions", body={"code": code})
        bank = resp.get("aspsp", {}).get("name") or getattr(self, "_bank_name", "Bank")
        accounts = []
        for a in resp.get("accounts", []):
            if not a.get("uid") or a.get("cash_account_type") == "LOAN":
                continue
            iban = (a.get("account_id") or {}).get("iban") or ""
            tail = f" ****{iban[-4:]}" if iban else ""
            accounts.append(dict(
                uid=a["uid"], id=f"eb-{a['identification_hash'][:12]}",
                name=f"{bank}{tail}" + (f" · {a['product']}" if a.get("product") else ""),
                currency=a["currency"], provider=bank.lower(),
                kind="savings" if a.get("cash_account_type") == "SVGS" else "cash"))
        valid_until = resp.get("access", {}).get("valid_until") or \
            (self._now() + MAX_CONSENT).isoformat()
        self.secrets["eb_session"] = {"session_id": resp["session_id"], "accounts": accounts,
                                      "valid_until": valid_until, "initial_done": False,
                                      "psu_headers": getattr(self, "_required_headers", None)}
        self._state = None

    # --- "user is present" headers and request budget --------------------
    def _required_psu_headers(self, session: dict) -> list[str]:
        if session.get("psu_headers") is None:  # session created before this was recorded
            try:
                session["psu_headers"] = self._find_bank().get("required_psu_headers") or []
            except Exception:
                return []
        return session["psu_headers"]

    def _psu_headers(self, session: dict) -> dict:
        """All the headers the bank requires, filled from the real request, or none at all."""
        if not self.psu:
            return {}  # nobody is present (not a user-triggered sync): send nothing
        required = self._required_psu_headers(session)
        if not required:
            return {}
        available = {k.lower(): v for k, v in self.psu.items() if v}
        headers = {}
        for name in required:
            if name.lower() not in available:
                return {}  # the API wants all or none; do not invent values
            headers[name] = available[name.lower()]
        return headers

    def _check_budget(self, session: dict, attended: bool) -> None:
        last = self.secrets.get("eb_last_fetch")
        if not last:
            return
        calls = 2 * max(len(session["accounts"]), 1)
        if attended:
            wait = ATTENDED_MIN_INTERVAL
        else:
            hours = float(self.secrets.get("eb_min_interval_hours") or
                          min(24, max(1, 24 * calls / UNATTENDED_DAILY_BUDGET)))
            wait = timedelta(hours=hours)
        due = _parse_time(last) + wait
        if self._now() < due:
            minutes = int((due - self._now()).total_seconds() // 60) + 1
            raise Skipped(f"Santander data was fetched recently; the bank limits how often an app "
                          f"may read it. Next sync possible in about {minutes} min.")

    # --- data -------------------------------------------------------------
    def _balance(self, uid: str, headers: dict) -> Decimal:
        balances = self._http("GET", f"/accounts/{uid}/balances", headers=headers).get("balances", [])
        if not balances:
            return Decimal(0)
        by_type = {b.get("balance_type"): b for b in balances}
        chosen = next((by_type[t] for t in BALANCE_PREFERENCE if t in by_type), balances[0])
        return Decimal(str(chosen["balance_amount"]["amount"]))

    def _transactions(self, acct: dict, date_from: str, headers: dict) -> list[dict]:
        out, seen, key = [], Counter(), None
        while True:
            params = {"date_from": date_from}
            if key:
                params["continuation_key"] = key
            page = self._http("GET", f"/accounts/{acct['uid']}/transactions", params=params,
                              headers=headers)
            for tx in page.get("transactions", []):
                mapped = map_transaction(tx, acct["id"], seen)
                if mapped:
                    out.append(mapped)
            key = page.get("continuation_key")
            if not key:
                return out

    def fetch(self, user_input: str | None = None) -> Snapshot:
        for k in ("eb_app_id", "eb_private_key"):
            if not self.secrets.get(k):
                raise RuntimeError(f"Missing {k}: run `finances secret {k}`")
        if user_input:
            self._create_session(user_input)
        if not self._session_alive():
            self._start_authorization()  # raises NeedsInput

        session = self.secrets["eb_session"]
        headers = self._psu_headers(session)
        if not user_input:  # right after approving there is nothing recent to protect
            self._check_budget(session, attended=bool(headers))
        days = LATER_SYNC_DAYS if session.get("initial_done") else FIRST_SYNC_DAYS
        date_from = (self._now() - timedelta(days=days)).date().isoformat()
        now = self._now().isoformat(timespec="seconds")
        snap = Snapshot()
        try:
            for acct in session["accounts"]:
                balance = self._balance(acct["uid"], headers)
                snap.accounts.append(dict(
                    id=acct["id"], provider=acct["provider"], name=acct["name"],
                    kind=acct["kind"], currency=acct["currency"], balance=float(balance),
                    updated_at=now))
                snap.transactions += self._transactions(acct, date_from, headers)
        except EnableBankingError as e:
            if e.status == 429:
                self.secrets["eb_last_fetch"] = self._now().isoformat()  # start the cool-down
                raise ConnectorError(
                    f"{e}. Banks may allow only ~4 background reads per day; wait up to 24 hours. "
                    "Syncs are now spaced out to stay within that.") from None
            if e.status in (403, 404, 422):  # consent revoked or closed by the bank
                self.secrets.pop("eb_session", None)
                self._start_authorization()
            raise
        session["initial_done"] = True
        self.secrets["eb_last_fetch"] = self._now().isoformat()
        return snap

    def field_report(self, days: int = 45) -> dict:
        """Which fields the bank actually fills in. Returns names and counts only, never values."""
        session = self.secrets.get("eb_session")
        if not (session and self._session_alive()):
            raise RuntimeError("No valid bank session: run a sync first")
        date_from = (self._now() - timedelta(days=days)).date().isoformat()
        counts, total = Counter(), 0

        def walk(prefix, value):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(f"{prefix}.{k}" if prefix else k, v)
            elif value not in (None, "", [], {}):
                counts[prefix] += 1

        for acct in session["accounts"]:
            key = None
            while True:
                params = {"date_from": date_from, **({"continuation_key": key} if key else {})}
                page = self._http("GET", f"/accounts/{acct['uid']}/transactions", params=params,
                                  headers=self._psu_headers(session))
                for tx in page.get("transactions", []):
                    total += 1
                    walk("", tx)
                key = page.get("continuation_key")
                if not key:
                    break
        return {"transactions": total, "fields": dict(counts.most_common())}
