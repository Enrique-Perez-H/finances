"""Trade Republic through the unofficial `pytr` library (read-only calls only).

Unofficial: Trade Republic has no public API, so this can break when they change
their app, and may conflict with their terms. Only portfolio and cash are read; no
order, savings-plan or watchlist call is ever made.

Secrets used: `tr_phone` (+34...), `tr_pin`. The login session is kept encrypted in
`tr_cookies` so that most syncs need no second factor.

Login flow (pytr's "v2" web login):
  * authenticator-app accounts -> NeedsInput(field="code"), user types the code;
  * otherwise the login is confirmed inside the Trade Republic app, and
    `fetch` waits for that confirmation (up to ~2 minutes).
"""
import asyncio
import sys
import types
from decimal import Decimal

from .base import Connector, NeedsInput, Snapshot

PROVIDER = "trade_republic"
CASH_ID = "tr-cash"
PORTFOLIO_ID = "tr-portfolio"
COOKIE_FIELDS = ("name", "value", "domain", "path", "secure", "expires")


def _load_pytr():
    """Import pytr lazily. `curl_cffi` is only needed for pytr's optional WAF-token
    strategy, which the v2 login does not use, and it cannot be installed on Termux,
    so a stand-in module is registered when it is missing."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        stub = types.ModuleType("curl_cffi")
        stub.requests = types.ModuleType("curl_cffi.requests")
        sys.modules["curl_cffi"] = stub
        sys.modules["curl_cffi.requests"] = stub.requests
    try:
        from pytr.api import TradeRepublicApi
        from pytr.portfolio import Portfolio
    except ImportError as e:
        raise RuntimeError(
            "pytr is not installed (see docs/TERMUX.md, section Trade Republic)") from e
    return TradeRepublicApi, Portfolio


def dump_cookies(jar) -> list[dict]:
    return [{k: getattr(c, k) for k in COOKIE_FIELDS} for c in jar]


def load_cookies(jar, rows: list[dict]) -> None:
    for r in rows:
        jar.set(r["name"], r["value"], domain=r["domain"], path=r["path"],
                secure=r["secure"], expires=r["expires"])


def build_snapshot(positions: list[dict], cash: list[dict], now: str) -> Snapshot:
    """Pure function: pytr's portfolio/cash structures -> our snapshot."""
    snap = Snapshot()
    cash_amount = Decimal(cash[0]["amount"]) if cash else Decimal(0)
    currency = cash[0].get("currencyId", "EUR") if cash else "EUR"
    rows, total = [], Decimal(0)
    for pos in positions:
        size = Decimal(pos["netSize"])
        value = Decimal(pos["netValue"])
        total += value
        rows.append(dict(account_id=PORTFOLIO_ID, symbol=pos.get("name") or pos["instrumentId"],
                         quantity=float(size), value=float(value),
                         cost_basis=float(Decimal(pos["averageBuyIn"]) * size)))
    snap.accounts = [
        dict(id=CASH_ID, provider=PROVIDER, name="Trade Republic Cash", kind="cash",
             currency=currency, balance=float(cash_amount), updated_at=now),
        dict(id=PORTFOLIO_ID, provider=PROVIDER, name="Trade Republic Portfolio",
             kind="investment", currency=currency, balance=float(total), updated_at=now),
    ]
    snap.holdings[PORTFOLIO_ID] = rows
    return snap


class TradeRepublicConnector(Connector):
    name = PROVIDER

    def __init__(self, secrets: dict, api_factory=None, portfolio_factory=None, now=None):
        super().__init__(secrets)
        self._api_factory = api_factory
        self._portfolio_factory = portfolio_factory
        self._now = now
        self.tr = None  # kept between calls while a login is waiting for a code

    def _classes(self):
        if self._api_factory and self._portfolio_factory:
            return self._api_factory, self._portfolio_factory
        return _load_pytr()

    def _login(self, user_input):
        Api, _ = self._classes()
        if self.tr is not None and not user_input and self.tr.weblogin_needs_authenticator:
            self.tr = None  # an old login is still waiting for a code nobody gave: restart
        if self.tr is None:
            phone, pin = self.secrets.get("tr_phone"), self.secrets.get("tr_pin")
            if not (phone and pin):
                raise RuntimeError("Missing tr_phone / tr_pin: run `finances secret tr_phone` "
                                   "and `finances secret tr_pin`")
            tr = Api(phone_no=str(phone), pin=str(pin), save_cookies=False, use_v2_login=True)
            if self.secrets.get("tr_cookies"):
                load_cookies(tr._websession.cookies, self.secrets["tr_cookies"])
                try:
                    tr.settings()  # cheap authenticated call: is the saved session alive?
                    self.tr = tr
                    return
                except Exception:
                    tr._websession.cookies.clear()
            tr.initiate_weblogin()
            self.tr = tr
            if tr.weblogin_needs_authenticator and not user_input:
                raise NeedsInput("Enter the code from your authenticator app", field="code")
        try:
            self.tr.complete_weblogin(user_input)
        except Exception:
            self.tr = None  # the login process is spent; start over next time
            raise
        self.secrets["tr_cookies"] = dump_cookies(self.tr._websession.cookies)

    def fetch(self, user_input: str | None = None) -> Snapshot:
        from datetime import datetime, timezone
        self._login(user_input)
        _, Portfolio = self._classes()
        tr = self.tr
        self.tr = None  # next sync starts from the saved session
        pf = Portfolio(tr)
        asyncio.run(pf.portfolio_loop())
        now = self._now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        return build_snapshot(pf.positions, pf.cash, now)
