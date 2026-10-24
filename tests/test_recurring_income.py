import unittest
from datetime import date, timedelta

from isolated import IsolatedHome
from finances import income, recurring
from finances.db import Database
from finances.sync import run_sync

TODAY = date(2026, 6, 15)


def add(db, n, day, amount, desc, category="uncategorized"):
    db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                           balance=0, updated_at="t"))
    db.upsert_transaction(dict(id=f"t{n}", account_id="a", date=day.isoformat(), amount=amount,
                               description=desc, category=category))


class RecurringTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")

    def monthly(self, desc, amount, count=5, start=date(2026, 1, 5)):
        for k in range(count):
            add(self.db, f"{desc}{k}", start + timedelta(days=30 * k), amount, desc)

    def test_monthly_detected_with_next_date(self):
        self.monthly("NETFLIX.COM", -12.99)
        (item,) = recurring.detect(self.db, TODAY)
        self.assertEqual((item["cadence"], item["direction"]), ("monthly", "out"))
        self.assertEqual(item["typical_amount"], 12.99)
        self.assertEqual(item["next_date"], (date(2026, 1, 5) + timedelta(days=120 + 30)).isoformat())
        self.assertTrue(item["active"])

    def test_irregular_and_too_few_not_detected(self):
        for k, gap in enumerate([0, 3, 40, 47, 90]):
            add(self.db, k, date(2026, 1, 1) + timedelta(days=gap), -10, "RANDOM SHOP")
        self.monthly("TWICE ONLY", -5, count=2)
        self.assertEqual(recurring.detect(self.db, TODAY), [])

    def test_variable_amounts_flagged_and_ended_marked(self):
        for k, amt in enumerate([-30, -80, -45, -120, -60]):
            add(self.db, k, date(2026, 1, 3) + timedelta(days=30 * k), amt, "ELECTRIC CO")
        (item,) = recurring.detect(self.db, TODAY)
        self.assertTrue(item["variable"])
        (item,) = recurring.detect(self.db, date(2027, 6, 1))
        self.assertFalse(item["active"])

    def test_income_and_directions_kept_apart(self):
        self.monthly("NOMINA ACME", 2000)
        self.monthly("ACME REFUND SHOP", -50)
        s = recurring.summary(self.db, TODAY)
        self.assertEqual(s["monthly_income"], 2000.0)
        self.assertEqual(s["monthly_costs"], 50.0)
        self.assertEqual(s["yearly_costs"], 600.0)

    def test_internal_transfers_ignored(self):
        for k in range(5):
            add(self.db, k, date(2026, 1, 5) + timedelta(days=30 * k), -300, "TO TRADE REPUBLIC", "Internal transfer")
        self.assertEqual(recurring.detect(self.db, TODAY), [])


class IncomeTests(IsolatedHome):
    def test_summary_groups_and_excludes_internal(self):
        db = Database("pw")
        for k in range(3):
            add(db, f"s{k}", date(2026, 3 + k, 28), 2000, "NOMINA ACME", "Salary")
        add(db, "b", date(2026, 5, 2), 30, "BIZUM DE ANA", "Transfers received")
        add(db, "i", date(2026, 5, 3), 500, "FROM MY OTHER ACCOUNT", "Internal transfer")
        s = income.summary(db, today=date(2026, 6, 10))
        self.assertEqual(s["total"], 6030.0)
        self.assertEqual(s["categories"][0]["category"], "Salary")
        self.assertEqual(s["monthly"][-1], {"month": "2026-05", "total": 2030.0, "salary": 2000.0,
                                            "other": 30.0})
        self.assertEqual(s["sources"][0]["count"], 3)

    def test_current_month_is_excluded(self):
        db = Database("pw")
        add(db, 1, date(2026, 5, 28), 2000, "NOMINA", "Salary")
        add(db, 2, date(2026, 6, 3), 99, "BIZUM", "Transfers received")
        s = income.summary(db, today=date(2026, 6, 10))
        self.assertEqual((s["months"], s["total"]), (["2026-05"], 2000.0))

    def test_empty(self):
        s = income.summary(Database("pw"))
        self.assertEqual((s["total"], s["categories"]), (0, []))

    def test_demo_data(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        cats = {c["category"] for c in income.summary(db)["categories"]}
        self.assertIn("Salary", cats)
        names = {i["key"] for i in recurring.detect(db)}
        self.assertTrue({"netflix com", "spotify ab", "basic fit", "nomina acme"} <= names, names)


if __name__ == "__main__":
    unittest.main()
