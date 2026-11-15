import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize, forecast, health, history, income, pairs, recurring, tricount, yearly
from finances.db import Database
from finances.server import make_handler

TODAY = date(2026, 11, 20)


class DuplicateTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=1000, updated_at="t"))
        self.n = 0
        self.rent = self.add("2026-10-01", -750, "ALQUILER", "Housing")
        self.shop = self.add("2026-10-12", -80, "MERCADONA", "Groceries")
        self.shop_twin = self.add("2026-10-12", -80, "MERCADONA", "Groceries")  # the same payment imported twice
        self.pay = self.add("2026-10-28", 2000, "NOMINA", "Salary")

    def add(self, day, amount, desc, category, account="a"):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id=account, date=day, amount=amount,
                                        description=desc, category=category, cat_source="manual"))
        return f"t{self.n}"

    def spending(self):
        return analysis.month_overview(self.db, "2026-10", TODAY)["spending"]

    def test_a_flagged_duplicate_stops_counting_everywhere(self):
        self.assertEqual(self.spending(), 750 + 160)
        self.assertEqual(categorize.set_duplicate(self.db, [self.shop_twin], True), 1)
        self.assertEqual(self.spending(), 750 + 80)
        cats = {c["category"]: c["total"] for c in analysis.spending_by_category(self.db, "2026-10")}
        self.assertEqual(cats["Groceries"], 80.0)
        self.assertEqual(analysis.top_entries(self.db, "2026-10", 5)["spends"][-1]["amount"], -80.0)
        self.assertEqual(analysis.average_month(self.db, 12, today=TODAY)["total"], 830.0)
        self.assertEqual(yearly.summary(self.db, "2026")["spending"]["total"], 830.0)

    def test_it_is_kept_visible_and_can_be_unflagged(self):
        categorize.set_duplicate(self.db, [self.shop_twin], True)
        listed = {e["id"]: e for e in analysis.entries(self.db, since="2026-10-01", until="2026-10-31")["items"]}
        self.assertTrue(listed[self.shop_twin]["duplicate"])
        self.assertEqual(listed[self.shop_twin]["category"], "Groceries")  # nothing else about it changed
        self.assertEqual(analysis.entries(self.db, since="2026-10-01", until="2026-10-31")["total_out"], 830.0)
        categorize.set_duplicate(self.db, [self.shop_twin], False)
        self.assertEqual(self.spending(), 750 + 160)

    def test_identical_twins_are_pointed_out_but_never_flagged_for_you(self):
        items = {e["id"]: e for e in analysis.entries(self.db, since="2026-10-01", until="2026-10-31")["items"]}
        self.assertTrue(items[self.shop]["possible_duplicate"] and items[self.shop_twin]["possible_duplicate"])
        self.assertFalse(items[self.rent]["possible_duplicate"])
        self.assertEqual(self.spending(), 750 + 160)  # a suspicion changes nothing until you decide
        found = {e["id"] for e in analysis.entries(self.db, flag="duplicates")["items"]}
        self.assertEqual(found, {self.shop, self.shop_twin})
        categorize.set_duplicate(self.db, [self.shop_twin], True)
        items = {e["id"]: e for e in analysis.entries(self.db, since="2026-10-01", until="2026-10-31")["items"]}
        self.assertFalse(items[self.shop]["possible_duplicate"])  # once one is flagged the other is no longer a twin
        self.assertEqual({e["id"] for e in analysis.entries(self.db, flag="duplicates")["items"]}, {self.shop_twin})

    def test_twins_need_the_same_account_date_amount_and_text(self):
        self.db.upsert_account(dict(id="b", provider="t", name="B", kind="cash", currency="EUR", balance=0, updated_at="t"))
        self.add("2026-10-12", -80, "MERCADONA", "Groceries", account="b")   # another account
        self.add("2026-10-13", -80, "MERCADONA", "Groceries")                 # another day
        self.add("2026-10-12", -80.5, "MERCADONA", "Groceries")               # another amount
        self.add("2026-10-12", -80, "CARREFOUR", "Groceries")                 # another text
        twins = {e["id"] for e in analysis.entries(self.db, flag="duplicates")["items"]}
        self.assertEqual(twins, {self.shop, self.shop_twin})

    def test_month_overview_counts_them(self):
        m = analysis.month_overview(self.db, "2026-10", TODAY)
        self.assertEqual(m["duplicates"], 2)
        categorize.set_duplicate(self.db, [self.shop_twin], True)
        self.assertEqual(analysis.month_overview(self.db, "2026-10", TODAY)["duplicates"], 1)

    def test_balance_history_ignores_a_duplicate_too(self):
        first = self.add("2026-11-05", -80, "FARMACIA", "Health")
        twin = self.add("2026-11-05", -80, "FARMACIA", "Health")  # imported twice: only one 80 left the account
        before = {r["month"]: r["cash"] for r in history.evolution(self.db, today=TODAY)["rows"]}
        categorize.set_duplicate(self.db, [twin], True)
        after = {r["month"]: r["cash"] for r in history.evolution(self.db, today=TODAY)["rows"]}
        self.assertEqual(before["2026-10"], 1000 + 160)  # undo both November payments
        self.assertEqual(after["2026-10"], 1000 + 80)    # the duplicate never really happened
        self.assertEqual(after["2026-11"], before["2026-11"])  # today's balance is the bank's number

    def test_other_features_leave_it_out(self):
        categorize.set_duplicate(self.db, [self.shop_twin], True)
        self.assertEqual(income.summary(self.db, today=TODAY)["total"], 2000.0)
        self.assertEqual(health.status(self.db, {})["unclear"], 0)
        # a flagged entry cannot be one half of a reimbursement pair
        a = self.add("2026-10-15", -60, "RESTAURANTE", "Restaurants & bars")
        b = self.add("2026-10-17", 60, "BIZUM DE LUIS", "Transfers received")
        pairs.apply(self.db)
        self.assertEqual(pairs.listing(self.db)["count"], 1)
        categorize.set_duplicate(self.db, [b], True)
        self.assertEqual(pairs.listing(self.db)["count"], 0)

    def test_unclear_entries_do_not_nag_for_a_duplicate(self):
        x = self.add("2026-10-20", -33, "PAGO XYZ", "uncategorized")
        self.assertEqual(health.status(self.db, {})["unclear"], 1)
        categorize.set_duplicate(self.db, [x], True)
        self.assertEqual(health.status(self.db, {})["unclear"], 0)
        self.assertEqual(categorize.review_queue(self.db), [])

    def test_exports_say_why_it_is_not_counted(self):
        import csv, io
        categorize.set_duplicate(self.db, [self.shop_twin], True)
        rows = list(csv.reader(io.StringIO(yearly.export_entries(self.db, "2026", "csv").decode()), delimiter=","))
        header = rows[0]
        flagged = [r for r in rows[1:] if r[header.index("Counted")] == "no"]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0][header.index("Not counted because")], "Duplicate")

    def test_validation(self):
        for bad in ([], ["x"] * 501):
            with self.assertRaises(ValueError):
                categorize.set_duplicate(self.db, bad, True)
        self.assertEqual(categorize.set_duplicate(self.db, ["ghost"], True), 0)

    def test_an_old_database_gets_the_column_and_views(self):
        self.db.conn.executescript("DROP VIEW counted; DROP VIEW counted_all;")
        self.db.conn.execute("ALTER TABLE transactions DROP COLUMN duplicate")
        self.db._migrate()
        self.assertIn("duplicate", {r["name"] for r in self.db.conn.execute("PRAGMA table_info(transactions)")})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM counted").fetchone()[0], 4)

    def test_endpoint(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, {}))
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
            self.assertEqual(call("/api/duplicate", {"tx_ids": [self.shop_twin], "duplicate": True}), (200, {"changed": 1}))
            self.assertEqual(call("/api/month?month=2026-10")[1]["spending"], 830.0)
            self.assertEqual(call("/api/duplicate", {"tx_ids": "x"})[0], 400)
            self.assertEqual(call("/api/duplicate", {"tx_ids": []})[0], 400)
            self.assertEqual(call("/api/duplicate", {"tx_ids": [self.shop_twin], "duplicate": False})[0], 200)
            self.assertEqual(call("/api/month?month=2026-10")[1]["spending"], 910.0)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
