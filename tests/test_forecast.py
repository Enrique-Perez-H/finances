import unittest
from datetime import date

from isolated import IsolatedHome
from finances import forecast
from finances.db import Database
from finances.sync import run_sync

TODAY = date(2026, 6, 15)


def seed(db, income=2000, rent=800, other_cost=300):
    for kind, bal in (("cash", 5000), ("savings", 1000), ("investment", 10000)):
        db.upsert_account(dict(id=kind, provider="t", name=kind, kind=kind, currency="EUR",
                               balance=bal, updated_at="t"))
    n = 0
    for month in range(1, 6):  # Jan..May completed months
        for day, amt, desc, cat in ((28, income, "NOMINA ACME", "Salary"),
                                    (1, -rent, "ALQUILER", "Housing"),
                                    (10, -other_cost, f"SHOP {month}", "Shopping")):
            n += 1
            db.upsert_transaction(dict(id=f"t{n}", account_id="cash", date=f"2026-{month:02d}-{day:02d}",
                                       amount=amt, description=desc, category=cat))


class ForecastTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        seed(self.db)

    def test_baseline(self):
        b = forecast.baseline(self.db, TODAY)
        self.assertEqual((b["income"], b["spending"]), (2000.0, 1100.0))
        self.assertEqual(b["recurring_income"], 2000.0)
        self.assertEqual(b["recurring_costs"], 800.0)  # rent recurs, the shops do not
        self.assertEqual(b["variable_costs"], 300.0)
        self.assertEqual((b["liquid"], b["invested"]), (6000.0, 10000.0))

    def test_flat_projection_adds_savings(self):
        f = forecast.project(self.db, months=12, inflation=0, annual_return=0, today=TODAY)
        self.assertEqual(f["rows"][0]["month"], "2026-07")
        self.assertEqual(f["rows"][-1]["liquid"], 6000 + 12 * 900)
        self.assertEqual(f["rows"][-1]["invested"], 10000.0)
        self.assertEqual(f["average_net"], 900.0)
        self.assertAlmostEqual(f["savings_rate"], 0.45)
        self.assertIsNone(f["runway_month"])

    def test_return_and_invest_share(self):
        f = forecast.project(self.db, months=12, inflation=0, annual_return=10, invest_share=100,
                             today=TODAY)
        last = f["rows"][-1]
        self.assertEqual(last["liquid"], 6000.0)  # all savings go to investments
        self.assertGreater(last["invested"], 10000 * 1.10)  # growth plus contributions

    def test_extra_cost_can_exhaust_cash(self):
        f = forecast.project(self.db, months=12, inflation=0, annual_return=0, extra_cost=2000,
                             today=TODAY)
        self.assertEqual(f["runway_month"], "2026-12")  # 6000 - 1100/mo drained by month 6

    def test_other_income_toggle(self):
        self.db.upsert_transaction(dict(id="gift", account_id="cash", date="2026-05-05", amount=600,
                                        description="BIZUM GIFT", category="Transfers received"))
        with_other = forecast.project(self.db, months=1, inflation=0, today=TODAY)["rows"][0]["income"]
        without = forecast.project(self.db, months=1, inflation=0, include_other_income=False,
                                   today=TODAY)["rows"][0]["income"]
        self.assertGreater(with_other, without)

    def test_validation(self):
        with self.assertRaises(ValueError):
            forecast.project(self.db, months=0, today=TODAY)
        with self.assertRaises(ValueError):
            forecast.project(self.db, annual_return=500, today=TODAY)

    def test_no_history_is_safe(self):
        f = forecast.project(Database("pw"), months=3, today=TODAY)
        self.assertEqual(f["baseline"]["months_used"], 0)
        self.assertEqual(f["end_total"], 0)

    def test_demo(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        f = forecast.project(db, months=24)
        self.assertEqual(len(f["rows"]), 24)
        self.assertGreater(f["baseline"]["months_used"], 0)


if __name__ == "__main__":
    unittest.main()
