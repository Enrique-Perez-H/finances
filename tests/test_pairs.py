import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize, income, pairs, recurring
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync


def r(id, day, amount, category="uncategorized"):
    return dict(id=id, date=day, amount=amount, category=category)


class FindPairsTests(unittest.TestCase):
    def test_exact_and_within_one_percent(self):
        self.assertEqual(pairs.find_pairs([r("s", "2026-01-01", -60), r("r", "2026-01-03", 60)]),
                         {"s": "r", "r": "s"})
        self.assertEqual(len(pairs.find_pairs([r("s", "2026-01-01", -100), r("r", "2026-01-03", 100.9)])), 2)
        self.assertEqual(pairs.find_pairs([r("s", "2026-01-01", -100), r("r", "2026-01-03", 98.5)]), {})

    def test_time_window(self):
        spend = r("s", "2026-01-10", -50)
        self.assertEqual(len(pairs.find_pairs([spend, r("r", "2026-03-01", 50)])), 2)   # 50 days later
        self.assertEqual(pairs.find_pairs([spend, r("r", "2026-03-20", 50)]), {})       # 69 days later
        self.assertEqual(len(pairs.find_pairs([spend, r("r", "2026-01-09", 50)])), 2)   # 1 day early
        self.assertEqual(pairs.find_pairs([spend, r("r", "2026-01-01", 50)]), {})       # long before

    def test_one_to_one_and_closest_wins(self):
        rows = [r("s1", "2026-01-01", -40), r("s2", "2026-01-02", -40), r("r", "2026-01-05", 40)]
        found = pairs.find_pairs(rows)
        self.assertEqual(found["r"], "s1")  # earliest spend takes it
        self.assertNotIn("s2", found)       # a receipt cancels only one spend
        rows = [r("s", "2026-01-01", -40), r("far", "2026-01-30", 40), r("near", "2026-01-02", 40)]
        self.assertEqual(pairs.find_pairs(rows)["s"], "near")

    def test_salary_and_interest_are_never_reimbursements(self):
        rows = [r("s", "2026-01-01", -2000), r("pay", "2026-01-02", 2000, "Salary")]
        self.assertEqual(pairs.find_pairs(rows), {})
        self.assertEqual(pairs.find_pairs([r("s", "2026-01-01", -5), r("i", "2026-01-02", 5, "Interest & dividends")]), {})


class IntegrationTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))

    def add(self, id, day, amount, desc, category="uncategorized"):
        self.db.upsert_transaction(dict(id=id, account_id="a", date=day, amount=amount,
                                        description=desc, category=category))

    def test_pair_is_left_out_of_every_total(self):
        self.add("s", "2026-01-05", -60, "RESTAURANTE LA PLAZA")
        self.add("r", "2026-01-07", 60, "BIZUM DE LUIS")
        self.add("x", "2026-01-08", -20, "MERCADONA")
        categorize.categorize_all(self.db)
        self.assertEqual(analysis.monthly_cashflow(self.db)[0]["spending"], 20.0)
        self.assertEqual(analysis.monthly_cashflow(self.db)[0]["income"], 0.0)
        self.assertEqual([c["category"] for c in analysis.spending_by_category(self.db, "2026-01")], ["Groceries"])
        self.assertEqual(income.summary(self.db, today=__import__("datetime").date(2026, 3, 1))["total"], 0)
        self.assertEqual([t["id"] for t in analysis.transactions(self.db, None, "2026-01")], ["x"])
        self.assertEqual(pairs.listing(self.db)["count"], 1)

    def test_paired_entries_do_not_clutter_the_review_queue(self):
        self.add("s", "2026-01-05", -60, "PAGO XYZ SL")
        self.add("r", "2026-01-07", 60, "TRANSF DE ABC")
        categorize.categorize_all(self.db)
        self.assertEqual(categorize.review_queue(self.db), [])

    def test_recurring_ignores_paired(self):
        for k, month in enumerate(("01", "02", "03", "04")):
            self.add(f"s{k}", f"2026-{month}-05", -30, "GYM CLUB")
            self.add(f"r{k}", f"2026-{month}-06", 30, "BIZUM DE LUIS")
        categorize.categorize_all(self.db)
        self.assertEqual(recurring.detect(self.db, __import__("datetime").date(2026, 5, 1)), [])

    def test_stable_and_block(self):
        self.add("s", "2026-01-05", -60, "RESTAURANTE LA PLAZA")
        self.add("r", "2026-01-07", 60, "BIZUM DE LUIS")
        self.assertGreater(categorize.categorize_all(self.db), 0)
        self.assertEqual(categorize.categorize_all(self.db), 0)  # nothing flickers
        pairs.block(self.db, "r")  # block from either side
        self.assertEqual(pairs.listing(self.db)["count"], 0)
        categorize.categorize_all(self.db)
        self.assertEqual(pairs.listing(self.db)["count"], 0)  # stays unpaired
        self.assertEqual(analysis.monthly_cashflow(self.db)[0]["spending"], 60.0)
        with self.assertRaises(ValueError):
            pairs.block(self.db, "ghost")

    def test_internal_transfers_are_not_candidates(self):
        self.add("o", "2026-01-05", -300, "TRANSFERENCIA A TRADE REPUBLIC")
        self.add("i", "2026-01-05", 300, "FROM SANTANDER", "Internal transfer")
        categorize.categorize_all(self.db)
        self.assertEqual(pairs.listing(self.db)["count"], 0)

    def test_balance_history_still_counts_paired_entries(self):
        from finances import history
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))
        self.add("s", "2026-04-05", -60, "RESTAURANTE")
        self.add("r", "2026-04-07", 60, "BIZUM")
        categorize.categorize_all(self.db)
        rows = {x["month"]: x for x in history.evolution(self.db, today=__import__("datetime").date(2026, 6, 20))["rows"]}
        self.assertEqual(rows["2026-04"]["cash"], 1000.0)  # money really moved; balances use everything

    def test_demo_has_a_pair_and_migration_adds_columns(self):
        run_sync(self.db, {}, ["demo"])
        self.assertGreater(pairs.listing(self.db)["count"], 0)
        self.db.conn.execute("ALTER TABLE transactions DROP COLUMN paired_with")
        self.db.conn.execute("ALTER TABLE transactions DROP COLUMN pair_blocked")
        self.db._migrate()
        cols = {c["name"] for c in self.db.conn.execute("PRAGMA table_info(transactions)")}
        self.assertTrue({"paired_with", "pair_blocked"} <= cols)


class ApiTests(IsolatedHome):
    def test_pairs_endpoints(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, {}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def call(path, body=None):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                                             data=json.dumps(body).encode() if body is not None else None,
                                             headers={"X-Requested-With": "finances"})
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())
            listing = call("/api/pairs")
            self.assertGreater(listing["count"], 0)
            call("/api/pairs/block", {"tx_id": listing["pairs"][0]["spend"]["id"]})
            self.assertEqual(call("/api/pairs")["count"], listing["count"] - 1)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
