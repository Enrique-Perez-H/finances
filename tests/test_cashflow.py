import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync

TODAY = date(2026, 6, 15)


class CashflowTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        self.n = 0

    def add(self, day, amount, category):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id="a", date=day, amount=amount,
                                        description=f"x{self.n}", category=category, cat_source="manual"))

    def seed(self):
        for month in ("2026-03", "2026-04", "2026-05"):
            self.add(f"{month}-02", 2000, "Salary")
            self.add(f"{month}-05", -600, "Groceries")
            self.add(f"{month}-09", -100, "Leisure")
        self.add("2026-04-20", -400, "Leisure")                       # an extra April outing
        self.add("2026-05-10", -9999, categorize.INTERNAL)             # never counted
        self.add("2026-05-11", -231000, categorize.CAPITAL)            # never counted
        self.add("2026-06-03", 2000, "Salary")                         # current month, incomplete

    def test_monthly_rows_and_partial_flag(self):
        self.seed()
        rows = analysis.monthly_cashflow(self.db, 600, today=TODAY)
        self.assertEqual([r["month"] for r in rows], ["2026-03", "2026-04", "2026-05", "2026-06"])
        self.assertEqual(rows[1], dict(month="2026-04", income=2000.0, spending=1100.0, saved=900.0, partial=False))
        self.assertEqual(rows[2]["spending"], 700.0)  # internal and capital excluded
        self.assertTrue(rows[3]["partial"])
        self.assertEqual(len(analysis.monthly_cashflow(self.db, 2, today=TODAY)), 2)

    def test_average_month_counts_missing_months_as_zero_and_skips_current(self):
        self.seed()
        avg = analysis.average_month(self.db, 12, today=TODAY)
        self.assertEqual((avg["months_used"], avg["first"], avg["last"]), (3, "2026-03", "2026-05"))
        cats = {c["category"]: c for c in avg["categories"]}
        self.assertEqual(cats["Groceries"]["average"], 600.0)
        self.assertEqual(cats["Leisure"]["average"], round((100 + 100 + 400 + 100) / 3, 2))
        self.assertEqual(avg["total"], round((700 + 1100 + 700) / 3, 2))
        self.assertAlmostEqual(sum(c["share"] for c in avg["categories"]), 1.0, places=2)
        self.assertEqual(avg["categories"][0]["category"], "Groceries")  # biggest first

    def test_average_window_and_empty(self):
        self.seed()
        last2 = analysis.average_month(self.db, 2, today=TODAY)
        self.assertEqual((last2["months_used"], last2["first"]), (2, "2026-04"))
        self.assertEqual(last2["total"], round((1100 + 700) / 2, 2))
        self.assertEqual(analysis.average_month(self.db, None, today=TODAY)["months_used"], 3)
        empty = analysis.average_month(Database("pw"), 12, today=TODAY)
        self.assertEqual((empty["months_used"], empty["categories"]), (0, []))

    def test_reimbursed_pairs_are_not_in_the_average(self):
        self.add("2026-05-01", -60, "Restaurants & bars")
        self.add("2026-05-03", 60, "Transfers received")
        categorize.categorize_all(self.db)
        self.assertEqual(analysis.average_month(self.db, 12, today=TODAY)["categories"], [])

    def test_top_entries_of_a_month(self):
        self.seed()
        for i, amt in enumerate((-50, -700, -20, -300), 1):
            self.add(f"2026-05-{10 + i}", amt, "Shopping")
        self.add("2026-05-20", 500, "Other income")
        self.add("2026-05-21", 30, "Transfers received")
        top = analysis.top_entries(self.db, "2026-05", 3)
        self.assertEqual([t["amount"] for t in top["spends"]], [-700, -600, -300])  # internal/capital skipped
        self.assertEqual([t["amount"] for t in top["incomes"]], [2000, 500, 30])
        self.assertEqual(top["spends"][0]["category"], "Shopping")
        self.assertEqual(len(analysis.top_entries(self.db, "2026-05", 1)["spends"]), 1)
        self.assertEqual(analysis.top_entries(self.db, "2030-01")["spends"], [])
        for bad in ("", "2026", "2026-5", "abcd-ef", "2026-05-01"):
            with self.assertRaises(ValueError):
                analysis.top_entries(self.db, bad)

    def test_top_ignores_reimbursed_pairs(self):
        self.add("2026-05-01", -80, "Restaurants & bars")
        self.add("2026-05-03", 80, "Transfers received")
        categorize.categorize_all(self.db)
        top = analysis.top_entries(self.db, "2026-05")
        self.assertEqual((top["spends"], top["incomes"]), ([], []))

    def test_endpoints(self):
        run_sync(self.db, {}, ["demo"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, {}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def get(path):
                with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}{path}") as r:
                    return json.loads(r.read())
            flow = get("/api/cashflow?months=600")
            self.assertTrue(flow and {"month", "income", "spending", "saved", "partial"} <= set(flow[0]))
            top = get("/api/top?month=" + flow[-2]["month"])
            self.assertEqual(set(top), {"month", "spends", "incomes"})
            self.assertLessEqual(len(top["spends"]), 3)
            avg = get("/api/spending/average?months=3")
            self.assertEqual(avg["months_used"], 3)
            self.assertGreater(avg["total"], 0)
            self.assertEqual(get("/api/spending/average?months=0")["months_used"] >= 3, True)  # 0 = all
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
