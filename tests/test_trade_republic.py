import unittest

from requests.cookies import RequestsCookieJar as CookieJar

from isolated import IsolatedHome
from finances import sync  # noqa: E402
from finances.connectors import REGISTRY  # noqa: E402
from finances.connectors.trade_republic import (  # noqa: E402
    TradeRepublicConnector, build_snapshot, dump_cookies, load_cookies)
from finances.db import Database  # noqa: E402

POSITIONS = [
    {"instrumentId": "IE00B4L5Y983", "name": "MSCI World", "netSize": "10.5",
     "averageBuyIn": "80", "netValue": "945.00"},
    {"instrumentId": "IE00BKM4GZ66", "name": "EM", "netSize": "2", "averageBuyIn": "30",
     "netValue": "70.50"},
]
CASH = [{"amount": "123.45", "currencyId": "EUR"}]


class FakeSession:
    cookies = CookieJar()


class FakeApi:
    """Stands in for pytr's TradeRepublicApi; records which login steps ran."""
    needs_code = True
    log: list = []

    def __init__(self, phone_no, pin, save_cookies, use_v2_login):
        assert use_v2_login and not save_cookies  # never write a plaintext cookie file
        self._websession = FakeSession()
        self._websession.cookies = CookieJar()

    @property
    def weblogin_needs_authenticator(self):
        return self.needs_code

    def initiate_weblogin(self):
        self.log.append("initiate")

    def complete_weblogin(self, code=None):
        if code == "bad":
            raise ValueError("That authenticator code is not correct.")
        self.log.append(f"complete:{code}")
        self._websession.cookies.set("session", "tok", domain=".traderepublic.com", path="/")

    def settings(self):
        raise RuntimeError("session expired")


class FakePortfolio:
    def __init__(self, tr):
        self.positions, self.cash = POSITIONS, CASH

    async def portfolio_loop(self):
        pass


def make(secrets):
    return TradeRepublicConnector(secrets, FakeApi, FakePortfolio, now="2026-01-01T00:00:00+00:00")


class SnapshotTests(unittest.TestCase):
    def test_build_snapshot(self):
        snap = build_snapshot(POSITIONS, CASH, "t")
        by_id = {a["id"]: a for a in snap.accounts}
        self.assertEqual(by_id["tr-cash"]["balance"], 123.45)
        self.assertEqual(by_id["tr-portfolio"]["balance"], 1015.5)
        rows = snap.holdings["tr-portfolio"]
        self.assertEqual(rows[0]["cost_basis"], 840.0)
        self.assertEqual({r["symbol"] for r in rows}, {"MSCI World", "EM"})

    def test_empty_account(self):
        snap = build_snapshot([], [], "t")
        self.assertEqual(snap.accounts[0]["balance"], 0)

    def test_cookie_roundtrip(self):
        jar = CookieJar()
        jar.set("a", "1", domain=".traderepublic.com", path="/")
        rows = dump_cookies(jar)
        jar2 = CookieJar()
        load_cookies(jar2, rows)
        self.assertEqual([(c.name, c.value) for c in jar2], [("a", "1")])


class FlowTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        FakeApi.log = []
        FakeApi.needs_code = True
        sync._pending.clear()

    def test_missing_credentials(self):
        with self.assertRaises(RuntimeError):
            make({}).fetch()

    def test_code_flow_then_session_saved(self):
        c = make({"tr_phone": "+34600000000", "tr_pin": "1234"})
        from finances.connectors.base import NeedsInput
        with self.assertRaises(NeedsInput) as ctx:
            c.fetch()
        self.assertEqual(ctx.exception.field, "code")
        snap = c.fetch(user_input="123456")
        self.assertEqual(len(snap.accounts), 2)
        self.assertEqual(FakeApi.log, ["initiate", "complete:123456"])
        self.assertEqual(c.secrets["tr_cookies"][0]["name"], "session")

    def test_wrong_code_restarts_login(self):
        c = make({"tr_phone": "+34600000000", "tr_pin": "1234"})
        from finances.connectors.base import NeedsInput
        with self.assertRaises(NeedsInput):
            c.fetch()
        with self.assertRaises(ValueError):
            c.fetch(user_input="bad")
        self.assertIsNone(c.tr)

    def test_app_confirmation_flow_needs_no_code(self):
        FakeApi.needs_code = False
        c = make({"tr_phone": "+34600000000", "tr_pin": "1234"})
        c.fetch()
        self.assertEqual(FakeApi.log, ["initiate", "complete:None"])

    def test_history_imported_and_marked_done(self):
        class TimelineApi(FakeApi):
            async def timeline_transactions(self, after=None):
                return "s"

            async def recv(self):
                return "s", {}, {"items": [{"id": "e1", "timestamp": "2026-05-02T08:00:00.000+0000",
                                            "title": "Zinsen", "subtitle": "", "eventType": "INTEREST_PAYOUT",
                                            "amount": {"value": 9.5}}], "cursors": {}}

            async def unsubscribe(self, sub):
                pass

        secrets = {"tr_phone": "+34600000000", "tr_pin": "1234"}
        c = TradeRepublicConnector(secrets, TimelineApi, FakePortfolio, now="t")
        from finances.connectors.base import NeedsInput
        with self.assertRaises(NeedsInput):
            c.fetch()
        snap = c.fetch(user_input="1")
        self.assertEqual([(t["amount"], t["category"]) for t in snap.transactions],
                         [(9.5, "Interest & dividends")])
        self.assertTrue(secrets["tr_history_done"])
        self.assertEqual(snap.notes, [])

    def test_history_failure_keeps_balances_and_says_so(self):
        secrets = {"tr_phone": "+34600000000", "tr_pin": "1234"}
        c = make(secrets)  # FakeApi has no timeline support: the call fails
        from finances.connectors.base import NeedsInput
        with self.assertRaises(NeedsInput):
            c.fetch()
        snap = c.fetch(user_input="1")
        self.assertEqual(len(snap.accounts), 2)
        self.assertEqual(snap.transactions, [])
        self.assertIn("history could not be read", snap.notes[0])
        self.assertNotIn("tr_history_done", secrets)

    def test_through_sync_layer_and_encrypted_persistence(self):
        saved = []
        REGISTRY["trade_republic"] = lambda secrets: make(secrets)
        try:
            db = Database("pw")
            secrets = {"tr_phone": "+34600000000", "tr_pin": "1234"}
            st = sync.run_sync(db, secrets, ["trade_republic"], persist_secrets=saved.append)
            self.assertEqual(st["trade_republic"]["state"], "needs_input")
            self.assertEqual(st["trade_republic"]["field"], "code")
            st = sync.run_sync(db, secrets, ["trade_republic"], {"trade_republic": "1"},
                               persist_secrets=saved.append)
            self.assertEqual(st["trade_republic"]["state"], "ok")
            self.assertEqual(len(saved), 1)  # cookies persisted exactly once
            names = [r["name"] for r in db.conn.execute("SELECT name FROM accounts")]
            self.assertIn("Trade Republic Cash", names)
        finally:
            from finances.connectors.trade_republic import TradeRepublicConnector as T
            REGISTRY["trade_republic"] = T


if __name__ == "__main__":
    unittest.main()
