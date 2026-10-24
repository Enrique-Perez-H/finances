import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import fund
from finances.db import Database
from finances.server import make_handler

PLAN = dict(name="Son", start_date="2026-01-01", initial=1000, monthly=100, annual_rate=0, years=2,
            contribution_growth=0, tax_rate=0)


class MathTests(unittest.TestCase):
    def plan(self, **kw):
        return fund.clean({**PLAN, **kw})

    def test_no_interest(self):
        p = fund.project(self.plan())
        self.assertEqual([y["balance"] for y in p["years"]], [2200.0, 3400.0])
        self.assertEqual((p["final"], p["contributed"], p["interest"]), (3400.0, 3400.0, 0.0))
        self.assertEqual([y["year"] for y in p["years"]], [2027, 2028])

    def test_effective_annual_rate_on_lump_sum(self):
        p = fund.project(self.plan(monthly=0, annual_rate=12, years=1))
        self.assertAlmostEqual(p["final"], 1120.0, places=2)  # (1+12%)^1, not 12 x 1%
        self.assertAlmostEqual(p["interest"], 120.0, places=2)

    def test_contribution_growth_applies_yearly(self):
        p = fund.project(self.plan(initial=0, contribution_growth=10))
        self.assertEqual(p["final"], 12 * 100 + 12 * 110)

    def test_tax_only_on_gain(self):
        p = fund.project(self.plan(monthly=0, annual_rate=10, years=1, tax_rate=20))
        self.assertAlmostEqual(p["tax"], 20.0, places=2)
        self.assertAlmostEqual(p["final_after_tax"], 1080.0, places=2)
        loss = fund.project(self.plan(monthly=0, annual_rate=-10, years=1, tax_rate=20))
        self.assertEqual(loss["tax"], 0)

    def test_scenarios_bracket_the_rate(self):
        s = fund.project(self.plan(annual_rate=3, years=10))["scenarios"]
        self.assertEqual([x["rate"] for x in s], [2, 3, 4])
        self.assertLess(s[0]["final"], s[1]["final"])
        self.assertLess(s[1]["final"], s[2]["final"])

    def test_required_monthly_hits_target(self):
        plan = self.plan(annual_rate=4, years=10, target=50000, monthly=0)
        need = fund.project(plan)["required_monthly"]
        reached = fund.project({**plan, "monthly": need})
        self.assertAlmostEqual(reached["final"], 50000, delta=1)
        done = fund.project(self.plan(target=100, years=1))  # already above target
        self.assertEqual(done["required_monthly"], 0.0)
        self.assertTrue(done["target_reached"])

    def test_validation(self):
        for bad in ({"name": ""}, {"years": 0}, {"annual_rate": 999}, {"monthly": -1},
                    {"start_date": "yesterday"}, {"initial": "abc"}, {"tax_rate": 70}):
            with self.assertRaises(ValueError, msg=str(bad)):
                fund.clean({**PLAN, **bad})


class StorageTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")

    def test_crud_and_linked_account(self):
        self.db.upsert_account(dict(id="acc", provider="t", name="Son fund", kind="investment",
                                    currency="EUR", balance=5000, updated_at="t"))
        plan = fund.save_plan(self.db, {**PLAN, "link_account_id": "acc"})
        (item,) = fund.list_plans(self.db)
        self.assertEqual(item["projection"]["initial"], 5000.0)  # starts from the real balance
        fund.save_plan(self.db, {**PLAN, "id": plan["id"], "name": "Renamed", "monthly": 0})
        self.assertEqual(fund.list_plans(self.db)[0]["plan"]["name"], "Renamed")
        fund.delete_plan(self.db, plan["id"])
        self.assertEqual(fund.list_plans(self.db), [])

    def test_unknown_link_and_plan_rejected(self):
        with self.assertRaises(ValueError):
            fund.save_plan(self.db, {**PLAN, "link_account_id": "ghost"})
        with self.assertRaises(ValueError):
            fund.save_plan(self.db, {**PLAN, "id": 99})

    def test_survives_encrypted_roundtrip(self):
        fund.save_plan(self.db, PLAN)
        self.db.save()
        self.assertEqual(fund.list_plans(Database("pw"))[0]["plan"]["name"], "Son")


class ApiTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Database("pw"), {}))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def call(self, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Requested-With": "finances"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_preview_save_list_delete(self):
        code, pv = self.call("/api/funds/preview", PLAN)
        self.assertEqual((code, pv["final"]), (200, 3400.0))
        code, plan = self.call("/api/funds", PLAN)
        self.assertEqual(code, 200)
        self.assertEqual(len(self.call("/api/funds")[1]), 1)
        self.assertEqual(self.call("/api/funds/delete", {"id": plan["id"]})[0], 200)
        self.assertEqual(self.call("/api/funds")[1], [])
        self.assertEqual(self.call("/api/funds", {**PLAN, "years": 99})[0], 400)


if __name__ == "__main__":
    unittest.main()
