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


class ChoiceTests(IsolatedHome):
    """The bank is the source of truth. A Tricount expense changes your figures only when you choose."""

    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="santander", name="Santander", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))
        self.bank("b1", "2026-10-05", -400, "GUARDERIA XYZ", "Kids")
        self.bank("b2", "2026-10-06", -30, "SOMETHING ELSE", "Other")

    def bank(self, id, day, amount, desc, category="uncategorized"):
        self.db.upsert_transaction(dict(id=id, account_id="a", date=day, amount=amount, description=desc,
                                        category=category, cat_source="manual"))

    def store(self, *entries):
        parsed = tricount.parse_registry(registry(list(entries)))
        return tricount.store(self.db, tricount.evaluate(parsed, ME)["expenses"], NOW.isoformat())

    def row(self, id):
        return self.db.conn.execute("SELECT * FROM transactions WHERE id=?", (id,)).fetchone()

    def choice(self, uid):
        return self.db.conn.execute("SELECT choice FROM tricount_expenses WHERE uuid=?", (uid,)).fetchone()[0]

    def spending(self, month="2026-10"):
        return {c["category"]: c["total"] for c in analysis.spending_by_category(self.db, month)}

    def test_nothing_counts_until_you_choose(self):
        out = self.store(DAYCARE, GAS)
        self.assertEqual((out["count"], out["pending"], out["counted_total"]), (2, 2, 0.0))
        self.assertIsNone(self.row("tri-g1"))
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM accounts WHERE id='tricount-shared'").fetchone())
        self.assertEqual(self.row("b1")["share"], 1.0)  # the bank entry is untouched
        self.assertEqual(self.spending()["Kids"], 400.0)

    def test_shared_counts_your_share(self):
        self.store(DAYCARE, GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        v = self.row("tri-g1")
        self.assertEqual((v["amount"], v["account_id"], v["category"], v["date"]),
                         (-50.0, "tricount-shared", "Utilities & telecom", "2026-10-10"))
        categorize.categorize_all(self.db)
        self.assertEqual(self.spending()["Utilities & telecom"], 50.0)
        self.assertEqual(tricount.summary(self.db)["counted_total"], 50.0)

    def test_only_mine_counts_all_of_it(self):
        self.store(DAYCARE, GAS)
        tricount.choose(self.db, ["g1"], "mine", NOW.isoformat())
        self.assertEqual(self.row("tri-g1")["amount"], -100.0)
        self.assertEqual(self.choice("g1"), "mine")

    def test_already_in_my_bank_adds_nothing(self):
        self.store(DAYCARE)
        tricount.choose(self.db, ["d1"], "skip")
        self.assertIsNone(self.row("tri-d1"))
        self.assertEqual(tricount.summary(self.db)["skipped"], 1)
        self.assertEqual(self.spending()["Kids"], 400.0)

    def test_changing_your_mind_and_taking_it_back(self):
        self.store(GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        tricount.choose(self.db, ["g1"], "mine", NOW.isoformat())
        self.assertEqual(self.row("tri-g1")["amount"], -100.0)
        tricount.choose(self.db, ["g1"], "", NOW.isoformat())
        self.assertIsNone(self.row("tri-g1"))
        self.assertEqual(self.choice("g1"), "")
        self.assertEqual(tricount.summary(self.db)["pending"], 1)

    def test_several_at_once_and_validation(self):
        self.store(DAYCARE, GAS)
        self.assertEqual(tricount.choose(self.db, ["d1", "g1"], "shared", NOW.isoformat()), 2)
        for bad in ((["g1"], "maybe"), ([], "shared"), (["ghost"], "shared")):
            with self.assertRaises(ValueError):
                tricount.choose(self.db, *bad)

    def test_running_the_sync_again_keeps_your_choices_and_adds_only_new_expenses(self):
        self.store(DAYCARE, GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        tricount.choose(self.db, ["d1"], "skip")
        snapshot = [tuple(r) for r in self.db.conn.execute("SELECT * FROM transactions ORDER BY id")]
        newer = entry("n1", "Pharmacy", 20, ME, {ME: 10, PARTNER: 10}, "2026-10-15")
        out = self.store(DAYCARE, GAS, newer)
        self.assertEqual((self.choice("g1"), self.choice("d1"), self.choice("n1")), ("shared", "skip", ""))
        self.assertEqual(out["pending"], 1)
        self.assertEqual(snapshot, [tuple(r) for r in self.db.conn.execute("SELECT * FROM transactions ORDER BY id")])

    def test_edits_in_the_group_follow_what_you_added(self):
        self.store(GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        self.store(entry("g1", "Gas bill", 120, PARTNER, {ME: 60, PARTNER: 60}, "2026-10-14", category="RENT_AND_UTILITIES"))
        v = self.row("tri-g1")
        self.assertEqual((v["amount"], v["date"]), (-60.0, "2026-10-14"))

    def test_deleting_in_the_group_removes_it_everywhere(self):
        self.store(GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        self.store()
        self.assertIsNone(self.row("tri-g1"))
        self.assertEqual(tricount.summary(self.db)["count"], 0)

    def test_an_expense_with_no_share_for_me_adds_nothing(self):
        free = entry("f1", "Gift for her", 60, ME, {PARTNER: 60}, "2026-10-04")
        self.store(free)
        tricount.choose(self.db, ["f1"], "shared", NOW.isoformat())
        self.assertIsNone(self.row("tri-f1"))  # my share is zero
        tricount.choose(self.db, ["f1"], "mine", NOW.isoformat())
        self.assertEqual(self.row("tri-f1")["amount"], -60.0)  # but it is a cost of mine if I say so

    def test_the_bank_duplicate_hint(self):
        out = self.store(DAYCARE, GAS)
        by = {e["uuid"]: e for e in out["expenses"]}
        hint = by["d1"]["in_bank"]
        self.assertEqual((hint["id"], hint["amount"]), ("b1", -400.0))  # same amount two days later
        self.assertIsNone(by["g1"]["in_bank"])  # your partner paid it: not in your bank
        self.assertEqual(tricount.summary(self.db)["pending"], 2)  # a hint, never a decision

    def test_the_hint_looks_only_at_close_dates_and_equal_amounts(self):
        self.bank("far", "2026-12-20", -250, "ELSEWHERE")
        wrong_amount = entry("w1", "Doctor", 260, ME, {ME: 130, PARTNER: 130}, "2026-12-18")
        far_date = entry("w2", "Taxi", 400, ME, {ME: 200, PARTNER: 200}, "2026-08-01")
        out = self.store(wrong_amount, far_date)
        self.assertEqual([e["in_bank"] for e in out["expenses"]], [None, None])

    def test_added_entries_stay_out_of_net_worth_and_balances(self):
        self.store(GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        self.assertEqual(analysis.net_worth(self.db)["total"], 1000.0)
        d = history.evolution(self.db, today=datetime(2026, 10, 20).date())
        self.assertNotIn("tricount-shared", [a["id"] for a in d["accounts"]])
        self.assertEqual(d["latest"]["total"], 1000.0)

    def test_disconnect_removes_what_was_added_but_keeps_bank_splits(self):
        self.store(DAYCARE, GAS)
        tricount.choose(self.db, ["g1"], "shared", NOW.isoformat())
        tricount.set_share(self.db, ["b1"], 0.5)
        tricount.disconnect(self.db)
        self.assertIsNone(self.row("tri-g1"))
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM accounts WHERE id='tricount-shared'").fetchone())
        self.assertEqual(self.row("b1")["share"], 0.5)  # your own split stays

    def test_migration_from_the_matching_version_keeps_what_counted(self):
        # an old database: linked, virtual and ignored expenses, and a bank entry split by the old matching
        self.db.conn.executescript("""
            DROP TABLE tricount_expenses;
            CREATE TABLE tricount_expenses (uuid TEXT PRIMARY KEY, date TEXT NOT NULL, description TEXT NOT NULL,
              total REAL NOT NULL, my_share REAL NOT NULL, payer TEXT NOT NULL, ignored INTEGER NOT NULL DEFAULT 0,
              tx_id TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0);
            INSERT INTO tricount_expenses VALUES('l', '2026-10-03', 'Daycare', 400, 200, 'me', 0, 'b1', 'linked', 0);
            INSERT INTO tricount_expenses VALUES('v', '2026-10-10', 'Gas', 100, 50, 'other', 0, 'tri-v', 'virtual', 0);
            INSERT INTO tricount_expenses VALUES('i', '2026-10-11', 'Left out', 10, 5, 'me', 1, '', '', 0);
            INSERT INTO tricount_expenses VALUES('n', '2026-10-12', 'Nothing yet', 10, 5, 'me', 0, '', '', 0);
            CREATE TABLE tricount_aliases (tri_key TEXT, bank_pattern TEXT);
            UPDATE transactions SET share=0.5, share_source='tricount' WHERE id='b1';""")
        self.db._migrate()
        got = {r["uuid"]: r["choice"] for r in self.db.conn.execute("SELECT uuid, choice FROM tricount_expenses")}
        self.assertEqual(got, {"l": "skip", "v": "shared", "i": "skip", "n": ""})
        b1 = self.row("b1")
        self.assertEqual((b1["share"], b1["share_source"]), (0.5, "manual"))  # the split is now simply yours
        self.assertIsNone(self.db.conn.execute("SELECT name FROM sqlite_master WHERE name='tricount_aliases'").fetchone())
        self.db._migrate()  # running it again changes nothing
        self.assertEqual({r["uuid"]: r["choice"] for r in self.db.conn.execute("SELECT uuid, choice FROM tricount_expenses")}, got)


class SplitBankEntriesTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR", balance=1000,
                                    updated_at="t"))
        for id, amount in (("g", -100), ("h", -60), ("inc", 500)):
            self.db.upsert_transaction(dict(id=id, account_id="a", date="2026-10-05", amount=amount,
                                            description=id, category="Other", cat_source="manual"))

    def test_split_in_half_and_back(self):
        self.assertEqual(tricount.set_share(self.db, ["g", "h"], 0.5), 2)
        counted = {r["id"]: r["amount"] for r in self.db.conn.execute("SELECT id, amount FROM counted")}
        self.assertEqual((counted["g"], counted["h"]), (-50.0, -30.0))
        self.assertEqual(analysis.spending_by_category(self.db, "2026-10")[0]["total"], 80.0)
        tricount.set_share(self.db, ["g"], 1)
        self.assertEqual(self.db.conn.execute("SELECT share, share_source FROM transactions WHERE id='g'").fetchone()[:],
                         (1.0, ""))

    def test_other_shares_and_validation(self):
        tricount.set_share(self.db, ["g"], 0.25)
        self.assertEqual(self.db.conn.execute("SELECT share FROM transactions WHERE id='g'").fetchone()[0], 0.25)
        for bad in (([], 0.5), (["g"], 1.5), (["g"], -0.1)):
            with self.assertRaises(ValueError):
                tricount.set_share(self.db, *bad)

    def test_the_browser_can_list_only_spending(self):
        out = analysis.entries(self.db, direction="out")
        self.assertEqual({e["id"] for e in out["items"]}, {"g", "h"})
        self.assertEqual({e["id"] for e in analysis.entries(self.db, direction="in")["items"]}, {"inc"})
        tricount.set_share(self.db, ["g"], 0.5)
        shown = {e["id"]: (e["amount"], e["raw_amount"], e["share"]) for e in analysis.entries(self.db, direction="out")["items"]}
        self.assertEqual(shown["g"], (-50.0, -100.0, 0.5))


class SyncTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="santander", name="Santander", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))

    def state(self, **kw):
        return {"token": "tAbC123xyz", "me": ME, **kw}

    def test_sync_lists_expenses_and_changes_no_figures(self):
        state = self.state()
        out = tricount.sync(self.db, state, tricount.TricountClient(state, FakeHttp(registry([DAYCARE, GAS, PAYBACK]))), NOW)
        self.assertEqual((out["count"], out["pending"], out["counted_total"]), (2, 2, 0.0))
        self.assertEqual((state["balance"], state["title"], state["synced_at"]), (0.0, "Home", "2026-10-20T12:00+00:00"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

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
        self.assertEqual((status["tricount"]["state"], status["tricount"]["to_review"]), ("ok", 2))
        self.assertIn("wait for your choice", status["tricount"]["note"])
        self.assertEqual(list(status), ["demo", "tricount"])
        self.assertTrue(saved)

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
        self.assertIn("does not know that link (HTTP 404)", status["tricount"]["error"])


class ApiTests(IsolatedHome):
    def test_connect_choose_split_disconnect(self):
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
            code, res = call("/api/tricount/connect", {"link": "https://tricount.com/FaBcDeFgHiJkLmNoP"})
            self.assertEqual((code, res["title"], len(res["members"])), (200, "Home", 2))
            self.assertEqual(call("/api/tricount/me", {"uuid": "nobody"})[0], 400)
            self.assertEqual(call("/api/tricount/me", {"uuid": ME})[0], 200)
            self.assertIn("tricount", secrets["sources"])
            code, status = call("/api/tricount/sync", {})
            self.assertEqual((code, status["tricount"]["state"]), (200, "ok"))
            info = call("/api/tricount")[1]
            self.assertEqual((info["configured"], info["connected_as"], info["pending"], info["counted_total"]), (True, ME, 2, 0.0))
            self.assertEqual(next(e for e in info["expenses"] if e["uuid"] == "d1")["in_bank"]["id"], "b1")
            self.assertEqual(call("/api/tricount/choose", {"uuids": ["g1"], "choice": "shared"})[1]["changed"], 1)
            self.assertEqual(call("/api/tricount/choose", {"uuids": ["d1"], "choice": "skip"})[0], 200)
            info = call("/api/tricount")[1]
            self.assertEqual((info["shared"], info["skipped"], info["pending"], info["counted_total"]), (1, 1, 0, 50.0))
            self.assertEqual(call("/api/tricount/choose", {"uuids": ["g1"], "choice": "maybe"})[0], 400)
            self.assertEqual(call("/api/tricount/choose", {"uuids": "g1", "choice": "shared"})[0], 400)
            self.assertEqual(call("/api/share", {"tx_ids": ["b1"], "share": 0.5})[1]["changed"], 1)  # split a bank entry
            self.assertEqual(call("/api/tricount/disconnect", {})[0], 200)
            self.assertNotIn("tricount", secrets)
            self.assertNotIn("tricount", secrets["sources"])
            self.assertEqual(db.conn.execute("SELECT share FROM transactions WHERE id='b1'").fetchone()[0], 0.5)
            self.assertIsNone(db.conn.execute("SELECT 1 FROM transactions WHERE id='tri-g1'").fetchone())
            self.assertEqual(call("/api/tricount/unknown", {})[0], 404)
        finally:
            tricount.TricountClient._urllib_http = original
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
