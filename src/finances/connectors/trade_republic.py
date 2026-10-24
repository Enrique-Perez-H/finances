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

from datetime import datetime, timedelta, timezone

from ..categorize import INTERNAL

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


TIMELINE_PAGES_FIRST, TIMELINE_PAGES_LATER = 40, 4
TIMELINE_DAYS_LATER = 60

# Event types whose meaning does not depend on the shop: their category is set here.
_INTEREST = {"INTEREST_PAYOUT", "INTEREST_PAYOUT_CREATED"}
_DIVIDEND = {"CREDIT", "SSP_CORPORATE_ACTION_CASH", "SSP_CORPORATE_ACTION_INVOICE_CASH"}
_MOVES = {  # money moving to/from your bank, or between cash and investments
    "ACCOUNT_TRANSFER_INCOMING", "BANK_TRANSACTION_INCOMING", "BANK_TRANSACTION_OUTGOING",
    "INCOMING_TRANSFER", "INCOMING_TRANSFER_DELEGATION", "OUTGOING_TRANSFER",
    "OUTGOING_TRANSFER_DELEGATION", "PAYMENT_INBOUND", "PAYMENT_INBOUND_APPLE_PAY",
    "PAYMENT_INBOUND_CREDIT_CARD", "PAYMENT_INBOUND_GOOGLE_PAY", "PAYMENT_INBOUND_SEPA_DIRECT_DEBIT",
    "PAYMENT-SERVICE-IN-PAYMENT-DIRECT-DEBIT", "PAYMENT_OUTBOUND", "JUNIOR_P2P_TRANSFER",
    "ORDER_EXECUTED", "SAVINGS_PLAN_EXECUTED", "SAVINGS_PLAN_INVOICE_CREATED", "TRADE_INVOICE",
    "TRADING_TRADE_EXECUTED", "TRADING_SAVINGSPLAN_EXECUTED", "IPO_TRADE_EXECUTED",
    "TRADE_CORRECTED", "SPARE_CHANGE_AGGREGATE", "BENEFITS_SPARE_CHANGE_EXECUTION",
    "SAVEBACK_AGGREGATE", "BENEFITS_SAVEBACK_EXECUTION", "ACQUISITION_TRADE_PERK",
    "PRIVATE_MARKET_FUND_TRADE_EXECUTED", "PRIVATE_MARKETS_TRADE_EXECUTED",
}
_TAX = {"SSP_TAX_CORRECTION", "SSP_TAX_CORRECTION_INVOICE", "TAX_CORRECTION", "TAX_REFUND"}
_ATM = {"CARD_SUCCESSFUL_ATM_WITHDRAWAL"}


def map_timeline_item(item: dict) -> dict | None:
    """One Trade Republic timeline entry -> one transaction on the cash account, or None."""
    if str(item.get("status", "")).lower() == "canceled":
        return None
    try:
        amount = Decimal(str((item.get("amount") or {}).get("value")))
    except Exception:
        return None
    if amount == 0 or not item.get("timestamp") or not item.get("id"):
        return None
    etype = str(item.get("eventType") or "").upper()
    hint = None
    if etype in _INTEREST or etype in _DIVIDEND:
        hint = "Interest & dividends" if amount > 0 else None
    elif etype in _MOVES:
        hint = INTERNAL
    elif etype in _TAX:
        hint = "Refunds" if amount > 0 else "Taxes"
    elif etype in _ATM:
        hint = "Cash withdrawals"
    text = " ".join(p for p in (item.get("title"), item.get("subtitle")) if p) or etype
    tx = dict(id=f"tr-{item['id']}", account_id=CASH_ID, date=str(item["timestamp"])[:10],
              amount=float(amount), description=text[:140], category="uncategorized")
    if hint:
        tx.update(category=hint, cat_source="connector")
    return tx


async def fetch_timeline(tr, since: str, max_pages: int) -> list[dict]:
    """Page through the timeline until entries get older than `since` (ISO date)."""
    out, after = [], None
    for _ in range(max_pages):
        sub = await tr.timeline_transactions(after)
        _, _, response = await tr.recv()
        await tr.unsubscribe(sub)
        reached_old = False
        for item in response.get("items", []):
            if str(item.get("timestamp", ""))[:10] < since:
                reached_old = True
                continue
            tx = map_timeline_item(item)
            if tx:
                out.append(tx)
        after = (response.get("cursors") or {}).get("after")
        if not after or reached_old:
            break
    return out


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
        first = not self.secrets.get("tr_history_done")
        since = "1970-01-01" if first else (
            datetime.now(timezone.utc) - timedelta(days=TIMELINE_DAYS_LATER)).date().isoformat()
        pages = TIMELINE_PAGES_FIRST if first else TIMELINE_PAGES_LATER
        result = {}

        async def run():
            try:  # history is a bonus: a failure here must not lose balances and holdings
                result["tx"] = await fetch_timeline(tr, since, pages)
            except Exception as e:
                result["error"] = type(e).__name__
            await pf.portfolio_loop()

        asyncio.run(run())
        now = self._now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        snap = build_snapshot(pf.positions, pf.cash, now)
        snap.transactions = result.get("tx", [])
        if "error" in result:
            snap.notes.append(f"Trade Republic history could not be read ({result['error']}); "
                              "balances are fine.")
        else:
            self.secrets["tr_history_done"] = True
        return snap
