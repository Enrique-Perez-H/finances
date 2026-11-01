import json
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize, history, tricount
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
ME, PARTNER = "me-uuid", "partner-uuid"


def member(uid, name):
    return {"RegistryMembershipNonUser": {"uuid": uid, "alias": {"display_name": name}}}


def entry(uid, desc, total, payer, shares, day, kind="NORMAL", status="ACTIVE", category="OTHER", sign=-1):
    return {"RegistryEntry": {
        "uuid": uid, "description": desc, "amount": {"value": str(sign * total), "currency": "EUR"},
        "membership_owned": {"RegistryMembershipNonUser": {"uuid": payer}},
        "allocations": [{"membership": {"RegistryMembershipNonUser": {"uuid": w}},
                         "amount": {"value": str(sign * a), "currency": "EUR"}, "type": "AMOUNT"}
                        for w, a in shares.items()],
        "date": f"{day} 09:00:00.000000", "status": status, "type_transaction": kind, "category": category}}


def registry(entries, currency="EUR"):
    return {"Response": [{"Registry": {
        "title": "Home", "currency": currency,
        "memberships": [member(ME, "Enrique"), member(PARTNER, "Partner")],
        "all_registry_entry": entries}}]}


DAYCARE = entry("d1", "Daycare", 400, ME, {ME: 200, PARTNER: 200}, "2026-10-03")
GAS = entry("g1", "Gas bill", 100, PARTNER, {ME: 50, PARTNER: 50}, "2026-10-10", category="RENT_AND_UTILITIES")
PAYBACK = entry("r1", "Settling up", 150, PARTNER, {ME: 150, PARTNER: 0}, "2026-10-12", kind="BALANCE", sign=1)
DELETED = entry("x1", "Deleted one", 80, ME, {ME: 40, PARTNER: 40}, "2026-10-01", status="INACTIVE")


class FakeHttp:
    def __init__(self, data, status=200):
        self.data, self.status, self.calls = data, status, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        if url.endswith("/session-registry-installation"):
            return 200, {"Response": [{"Token": {"token": "tok"}}, {"UserPerson": {"id": 77}}]}
        return self.status, self.data


class ParseTests(unittest.TestCase):
    def test_token_from_a_link_or_bare(self):
        ok = {
            "https://tricount.com/FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",     # real ones need not start with t
            "https://tricount.com/tAbC123xyz": "tAbC123xyz",
            "https://tricount.com/tAbC123xyz/?x=1": "tAbC123xyz",
            " FaBcDeFgHiJkLmNoP ": "FaBcDeFgHiJkLmNoP",                         # the bare code
            "https://tricount.com/en/FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",   # a language part
            "https://tricount.com/en-us/FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",
            "https://tricount.com/#/FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",    # a fragment
            "https://tricount.com/FaBc-12_3xYz": "FaBc-12_3xYz",                 # hyphen and underscore
            "http://tricount.com/FaBcDeFgHiJkLmNoP\n": "FaBcDeFgHiJkLmNoP",
            "Join my Tricount! https://tricount.com/FaBcDeFgHiJkLmNoP See you": "FaBcDeFgHiJkLmNoP",
            "https://tricount.com/FaBcDeFgHiJkLmNoP?utm_source=share#top": "FaBcDeFgHiJkLmNoP",
            "https%3A%2F%2Ftricount.com%2FFaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",
            "tricount.com/FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",               # no scheme
            "Here you go: FaBcDeFgHiJkLmNoP": "FaBcDeFgHiJkLmNoP",               # the code inside a sentence
        }
        for text, token in ok.items():
            self.assertEqual(tricount.extract_token(text), token, text)

    def test_unreadable_links_explain_their_shape_without_leaking_the_token(self):
        for bad in ("", "hello", "https://example.com/", "https://tricount.com/xyz", "https://tricount.com/share",
                    "just some ordinary words here", "tricount"):
            with self.assertRaises(tricount.TricountError, msg=bad):
                tricount.extract_token(bad)
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.extract_token("https://tricount.com/short")
        message = str(ctx.exception)
        self.assertIn("tricount.com", message)
        self.assertIn("s… (5 characters)", message)
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.extract_token("https://elsewhere.org/$$$$$$$$$$$$$$$$$")
        self.assertIn("elsewhere.org", str(ctx.exception))

    def test_parse_and_my_shares_and_balance(self):
        parsed = tricount.parse_registry(registry([DAYCARE, GAS, PAYBACK, DELETED]))
        self.assertEqual(parsed["members"], [{"uuid": ME, "name": "Enrique"}, {"uuid": PARTNER, "name": "Partner"}])
        result = tricount.evaluate(parsed, ME)
        by = {e["uuid"]: e for e in result["expenses"]}
        self.assertEqual(set(by), {"d1", "g1"})  # settling-up and the deleted one are not expenses
        self.assertEqual((by["d1"]["total"], by["d1"]["my_share"], by["d1"]["payer_is_me"]), (400.0, 200.0, True))
        self.assertEqual((by["g1"]["total"], by["g1"]["my_share"], by["g1"]["payer_is_me"]), (100.0, 50.0, False))
        # I paid 400 and owe 200 (+200); she paid 100 of which I owe 50 (-50); she paid me back 150 (-150)
        self.assertEqual(result["balance"], 0.0)
        self.assertEqual(tricount.evaluate(parsed, PARTNER)["balance"], 0.0)
        before_payback = tricount.evaluate(tricount.parse_registry(registry([DAYCARE, GAS])), ME)["balance"]
        self.assertEqual(before_payback, 150.0)  # she owes me 150

    def test_registry_without_a_group_is_an_error(self):
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.parse_registry({"Response": [{"UserPerson": {"id": 1}}]})
        self.assertIn("answered with: UserPerson", str(ctx.exception))

    def test_odd_entries_do_not_crash(self):
        odd = {"RegistryEntry": {"uuid": "o", "amount": {"value": "abc"}}}
        self.assertEqual(tricount.parse_registry(registry([odd, {"Other": {}}]))["entries"], [])


class ClientTests(unittest.TestCase):
    def test_authenticates_then_reads_with_the_link_only(self):
        http = FakeHttp(registry([DAYCARE]))
        state = {}
        parsed = tricount.TricountClient(state, http).fetch("tAbC123xyz")
        self.assertEqual(parsed["title"], "Home")
        method, url, headers, body = http.calls[0]
        self.assertEqual((method, url.endswith("/v1/session-registry-installation")), ("POST", True))
        self.assertTrue(body["client_public_key"].startswith("-----BEGIN RSA PUBLIC KEY-----"))
        self.assertEqual(state["app"]["app_id"], body["app_installation_uuid"])  # identity is kept for next time
        method, url, headers, _ = http.calls[1]
        self.assertEqual(method, "GET")  # read only: nothing is ever written to the group
        self.assertIn("/v1/user/77/registry?public_identifier_token=tAbC123xyz", url)
        self.assertEqual(headers["X-Bunq-Client-Authentication"], "tok")
        self.assertEqual({c[0] for c in http.calls}, {"POST", "GET"})
        self.assertEqual([c[1] for c in http.calls if c[0] == "POST"], [http.calls[0][1]])  # only the sign-in

    def test_errors_are_explained(self):
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.TricountClient({}, FakeHttp({}, 404)).fetch("tAbC123xyz")
        self.assertIn("does not know that link (HTTP 404)", str(ctx.exception))
        with self.assertRaises(tricount.TricountError):
            tricount.TricountClient({}, FakeHttp({}, 500)).fetch("tAbC123xyz")

        def refuse(method, url, headers, body=None):
            return 403, {}
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.TricountClient({}, refuse).fetch("tAbC123xyz")
        self.assertIn("may have changed", str(ctx.exception))


class BooksTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="santander", name="Santander", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))
        self.bank("b1", "2026-10-05", -400, "GUARDERIA XYZ", "Kids")
        self.bank("b0", "2026-09-05", -400, "GUARDERIA XYZ", "Kids")  # last month's, a different expense
        self.bank("b2", "2026-10-06", -30, "SOMETHING ELSE", "Other")

    def bank(self, id, day, amount, desc, category="uncategorized"):
        self.db.upsert_transaction(dict(id=id, account_id="a", date=day, amount=amount, description=desc,
                                        category=category, cat_source="manual"))

    def apply(self, entries):
        parsed = tricount.parse_registry(registry(entries))
        return tricount.apply(self.db, tricount.evaluate(parsed, ME)["expenses"], NOW.isoformat())

    def row(self, id):
        return self.db.conn.execute("SELECT * FROM transactions WHERE id=?", (id,)).fetchone()

    def test_my_share_of_what_i_paid_counts_on_the_bank_entry(self):
        out = self.apply([DAYCARE, GAS])
        b1 = self.row("b1")
        self.assertEqual((b1["share"], b1["share_source"]), (0.5, "tricount"))
        self.assertEqual(self.row("b0")["share"], 1.0)  # a different month's payment is not touched
        self.assertEqual((out["linked"], out["virtual"]), (1, 1))
        counted = self.db.conn.execute("SELECT amount, raw_amount FROM counted WHERE id='b1'").fetchone()
        self.assertEqual((counted["amount"], counted["raw_amount"]), (-200.0, -400.0))

    def test_what_my_partner_paid_appears_as_my_share(self):
        self.apply([DAYCARE, GAS])
        v = self.row("tri-g1")
        self.assertEqual((v["amount"], v["account_id"], v["category"], v["date"]), (-50.0, "tricount-shared",
                                                                                   "Utilities & telecom", "2026-10-10"))
        acct = self.db.conn.execute("SELECT kind, balance FROM accounts WHERE id='tricount-shared'").fetchone()
        self.assertEqual((acct["kind"], acct["balance"]), ("shared", 0))

    def test_totals_use_my_share_everywhere(self):
        self.apply([DAYCARE, GAS])
        categorize.categorize_all(self.db)
        cats = {c["category"]: c["total"] for c in analysis.spending_by_category(self.db, "2026-10")}
        self.assertEqual(cats["Kids"], 200.0)
        self.assertEqual(cats["Utilities & telecom"], 50.0)
        self.assertEqual(analysis.monthly_cashflow(self.db, 12, today=datetime(2026, 11, 5).date())[-1]["spending"], 280.0)
        self.assertEqual(analysis.top_entries(self.db, "2026-10", 1)["spends"][0]["amount"], -200.0)

    def test_virtual_account_is_not_money(self):
        self.apply([DAYCARE, GAS])
        self.assertEqual(analysis.net_worth(self.db)["total"], 1000.0)
        d = history.evolution(self.db, today=datetime(2026, 10, 20).date())
        self.assertNotIn("tricount-shared", [a["id"] for a in d["accounts"]])
        self.assertEqual(d["latest"]["total"], 1000.0)
        self.assertNotIn("tricount-shared", [a["id"] for a in analysis.summary(self.db)["accounts"]])

    def test_running_it_again_changes_nothing(self):
        self.apply([DAYCARE, GAS])
        snap = [tuple(r) for r in self.db.conn.execute("SELECT * FROM transactions ORDER BY id")]
        self.apply([DAYCARE, GAS])
        self.assertEqual(snap, [tuple(r) for r in self.db.conn.execute("SELECT * FROM transactions ORDER BY id")])

    def test_amount_or_split_edits_in_the_group_follow(self):
        self.apply([DAYCARE, GAS])
        self.apply([entry("d1", "Daycare", 400, ME, {ME: 100, PARTNER: 300}, "2026-10-03"), GAS])
        self.assertEqual(self.row("b1")["share"], 0.25)
        self.apply([entry("d1", "Daycare", 420, ME, {ME: 210, PARTNER: 210}, "2026-10-03"), GAS])
        self.assertEqual(self.row("b1")["share"], 1.0)  # 420 no longer matches the 400 payment: unlinked
        self.assertEqual(self.row("tri-d1")["amount"], -210.0)  # so its share counts as a virtual entry

    def test_deleting_in_the_group_restores_everything(self):
        self.apply([DAYCARE, GAS])
        self.apply([])
        self.assertEqual(self.row("b1")["share"], 1.0)
        self.assertIsNone(self.row("tri-g1"))
        self.assertEqual(tricount.summary(self.db)["count"], 0)

    def test_nothing_in_my_share_means_nothing_to_count(self):
        free = entry("f1", "Gift for her", 60, ME, {PARTNER: 60}, "2026-10-04")
        self.bank("bg", "2026-10-04", -60, "FLORES")
        self.apply([free])
        self.assertEqual(self.row("bg")["share"], 1.0)  # I paid it all for her, but a share of 0 is not applied
        self.assertIsNone(self.row("tri-f1"))

    def test_unmatched_payment_counts_then_matches_when_the_bank_entry_arrives(self):
        card = entry("c1", "Pediatrician", 90, ME, {ME: 45, PARTNER: 45}, "2026-10-08")
        out = self.apply([card])
        self.assertEqual(len(out["unmatched_mine"]), 1)
        self.assertEqual(self.row("tri-c1")["amount"], -45.0)
        self.bank("b3", "2026-10-09", -90, "CLINICA PEDIATRIA")
        self.assertEqual(tricount.rematch(self.db), 1)
        self.assertIsNone(self.row("tri-c1"))
        self.assertEqual(self.row("b3")["share"], 0.5)
        self.assertEqual(tricount.summary(self.db)["unmatched_mine"], [])

    def test_matching_rules(self):
        near_amount = entry("n1", "Close but no", 400.0, ME, {ME: 200, PARTNER: 200}, "2026-10-03")
        self.bank("late", "2026-10-30", -400, "TOO LATE")  # 27 days after: outside the window
        self.db.conn.execute("DELETE FROM transactions WHERE id='b1'")
        self.apply([near_amount])
        self.assertEqual(self.row("late")["share"], 1.0)
        self.assertEqual(tricount.find_bank_match(self.db, "2026-10-03", 400.0, set()), None)
        self.bank("w1", "2026-10-04", -400.0, "A")
        self.bank("w2", "2026-10-03", -400.0, "B")
        self.assertEqual(tricount.find_bank_match(self.db, "2026-10-03", 400.0, set()), "w2")  # closest date
        self.assertEqual(tricount.find_bank_match(self.db, "2026-10-03", 400.0, {"w2"}), "w1")
        self.assertEqual(tricount.find_bank_match(self.db, "2026-10-03", 400.01, set()), "w2")  # a cent of slack

    def test_two_identical_monthly_expenses_each_take_their_own_payment(self):
        sep = entry("d0", "Daycare", 400, ME, {ME: 200, PARTNER: 200}, "2026-09-03")
        self.apply([DAYCARE, sep])
        self.assertEqual((self.row("b1")["share"], self.row("b0")["share"]), (0.5, 0.5))

    def test_ignoring_and_unignoring(self):
        self.apply([DAYCARE, GAS])
        tricount.set_ignored(self.db, "g1", True)
        tricount.set_ignored(self.db, "d1", True)
        self.assertEqual((self.row("b1")["share"], self.row("tri-g1")), (1.0, None))
        self.apply([DAYCARE, GAS])  # a later sync keeps respecting the choice
        self.assertEqual((self.row("b1")["share"], self.row("tri-g1")), (1.0, None))
        tricount.set_ignored(self.db, "g1", False)
        self.apply([DAYCARE, GAS])
        self.assertEqual(self.row("tri-g1")["amount"], -50.0)
        with self.assertRaises(ValueError):
            tricount.set_ignored(self.db, "ghost", True)

    def test_disconnect_puts_everything_back(self):
        self.apply([DAYCARE, GAS])
        tricount.disconnect(self.db)
        self.assertEqual(self.row("b1")["share"], 1.0)
        self.assertIsNone(self.row("tri-g1"))
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM accounts WHERE id='tricount-shared'").fetchone())

    def test_share_by_hand_without_tricount(self):
        self.assertEqual(tricount.set_share(self.db, ["b2"], 0.5), 1)
        self.assertEqual((self.row("b2")["share"], self.row("b2")["share_source"]), (0.5, "manual"))
        tricount.set_share(self.db, ["b2"], 1)
        self.assertEqual(self.row("b2")["share_source"], "")
        self.apply([DAYCARE])
        with self.assertRaises(ValueError):  # matched to Tricount: change it there
            tricount.set_share(self.db, ["b1"], 0.3)
        for bad in (([], 0.5), (["b2"], 1.5), (["b2"], -0.1)):
            with self.assertRaises(ValueError):
                tricount.set_share(self.db, *bad)

    def test_shared_entries_do_not_break_pairing_or_balances(self):
        self.apply([DAYCARE])
        categorize.categorize_all(self.db)
        d = history.evolution(self.db, today=datetime(2026, 10, 20).date())
        self.assertEqual(d["latest"]["cash"], 1000.0)  # the real money moved is still the full 400


class SyncTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="santander", name="Santander", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))
        self.db.upsert_transaction(dict(id="b1", account_id="a", date="2026-10-05", amount=-400,
                                        description="GUARDERIA", category="Kids", cat_source="manual"))

    def state(self, **kw):
        return {"token": "tAbC123xyz", "me": ME, **kw}

    def test_sync_applies_and_remembers(self):
        state = self.state()
        out = tricount.sync(self.db, state, tricount.TricountClient(state, FakeHttp(registry([DAYCARE, GAS, PAYBACK]))), NOW)
        self.assertEqual((out["count"], out["linked"], out["virtual"]), (2, 1, 1))
        self.assertEqual((state["balance"], state["title"], state["synced_at"]), (0.0, "Home", "2026-10-20T12:00+00:00"))

    def test_clear_errors(self):
        http = FakeHttp(registry([DAYCARE]))
        with self.assertRaises(tricount.TricountError):
            tricount.sync(self.db, {}, tricount.TricountClient({}, http), NOW)
        state = self.state(me="stranger")
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.sync(self.db, state, tricount.TricountClient(state, http), NOW)
        self.assertIn("choose who you are again", str(ctx.exception))
        state = self.state()
        with self.assertRaises(tricount.TricountError) as ctx:
            tricount.sync(self.db, state, tricount.TricountClient(state, FakeHttp(registry([DAYCARE], "USD"))), NOW)
        self.assertIn("only EUR", str(ctx.exception))

    def test_through_run_sync_after_the_banks(self):
        secrets = {"tricount": self.state()}
        original = tricount.TricountClient._urllib_http
        tricount.TricountClient._urllib_http = staticmethod(FakeHttp(registry([DAYCARE, GAS])))
        saved = []
        try:
            status = run_sync(self.db, secrets, ["tricount", "demo"], persist_secrets=saved.append)
        finally:
            tricount.TricountClient._urllib_http = original
        self.assertEqual(status["tricount"]["state"], "ok")
        self.assertEqual(status["demo"]["state"], "ok")
        self.assertEqual(list(status), ["demo", "tricount"])  # banks first, so the payments are there to match
        self.assertEqual(self.db.conn.execute("SELECT share FROM transactions WHERE id='b1'").fetchone()[0], 0.5)
        self.assertTrue(saved)  # the identity and results were persisted

    def test_a_failing_tricount_does_not_stop_the_rest(self):
        secrets = {"tricount": self.state()}
        original = tricount.TricountClient._urllib_http
        tricount.TricountClient._urllib_http = staticmethod(FakeHttp({}, 404))
        try:
            status = run_sync(self.db, secrets, ["demo", "tricount"])
        finally:
            tricount.TricountClient._urllib_http = original
        self.assertEqual(status["demo"]["state"], "ok")
        self.assertEqual(status["tricount"]["state"], "error")
        self.assertIn("does not know that link", status["tricount"]["error"])


class ApiTests(IsolatedHome):
    def test_connect_choose_sync_disconnect(self):
        db = Database("pw")
        db.upsert_account(dict(id="a", provider="santander", name="S", kind="cash", currency="EUR",
                               balance=1, updated_at="t"))
        db.upsert_transaction(dict(id="b1", account_id="a", date="2026-10-05", amount=-400, description="G",
                                   category="Kids", cat_source="manual"))
        secrets = {"sources": ["demo"]}
        original = tricount.TricountClient._urllib_http
        tricount.TricountClient._urllib_http = staticmethod(FakeHttp(registry([DAYCARE, GAS])))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, secrets))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            def call(path, body=None):
                req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                             headers={"X-Requested-With": "finances"})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            self.assertEqual(call("/api/tricount")[1]["configured"], False)
            self.assertEqual(call("/api/tricount/connect", {"link": "nonsense"})[0], 400)
            code, res = call("/api/tricount/connect", {"link": "https://tricount.com/tAbC123xyz"})
            self.assertEqual((code, res["title"], len(res["members"])), (200, "Home", 2))
            self.assertEqual(call("/api/tricount/me", {"uuid": "nobody"})[0], 400)
            self.assertEqual(call("/api/tricount/me", {"uuid": ME})[0], 200)
            self.assertIn("tricount", secrets["sources"])
            code, status = call("/api/tricount/sync", {})
            self.assertEqual((code, status["tricount"]["state"]), (200, "ok"))
            info = call("/api/tricount")[1]
            self.assertEqual((info["configured"], info["connected_as"], info["linked"], info["virtual"]), (True, ME, 1, 1))
            exp = next(e for e in info["expenses"] if e["uuid"] == "g1")
            self.assertEqual(call("/api/tricount/ignore", {"uuid": "g1", "ignored": True})[0], 200)
            self.assertEqual(call("/api/tricount")[1]["ignored"], 1)
            self.assertEqual(call("/api/share", {"tx_ids": ["b1"], "share": 0.3})[0], 400)  # matched to Tricount
            self.assertEqual(call("/api/tricount/disconnect", {})[0], 200)
            self.assertNotIn("tricount", secrets)
            self.assertNotIn("tricount", secrets["sources"])
            self.assertEqual(db.conn.execute("SELECT share FROM transactions WHERE id='b1'").fetchone()[0], 1.0)
            self.assertEqual(call("/api/tricount/unknown", {})[0], 404)
            self.assertEqual(call("/api/share", {"tx_ids": ["b1"], "share": 0.5})[1]["changed"], 1)
        finally:
            tricount.TricountClient._urllib_http = original
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
