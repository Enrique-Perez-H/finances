import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import recurring
from finances.db import Database
from finances.server import make_handler

TODAY = date(2026, 6, 15)
CLEANING = [("2025-12-22", -90), ("2026-01-05", -130), ("2026-02-27", -70),
            ("2026-03-09", -150), ("2026-04-30", -100), ("2026-05-04", -110)]


class IrregularTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        self.n = 0

    def add(self, day, amount, desc):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id="a", date=day, amount=amount,
                                        description=desc, category="uncategorized"))

    def cleaning(self, rows=CLEANING, desc="RECIBO LIMPIEZAS JACA SL"):
        for day, amount in rows:
            self.add(day, amount, desc)

    def found(self, **kw):
        return {i["key"]: i for i in recurring.detect(self.db, TODAY, **kw)}

    def test_monthly_bill_with_changing_dates_and_amounts_is_found(self):
        self.cleaning()
        item = self.found()["limpiezas jaca"]
        self.assertEqual((item["kind"], item["direction"], item["variable"]), ("presence", "out", True))
        self.assertEqual((item["amount_min"], item["amount_max"]), (70.0, 150.0))
        self.assertEqual(item["months_seen"], "6 of the last 6 months")
        self.assertEqual(item["monthly_equivalent"], round(650 / 6, 2))  # what it really costs per month
        self.assertTrue(item["active"])

    def test_wording_variants_still_group_together(self):
        for i, (day, amount) in enumerate(CLEANING):
            self.add(day, amount, ["TRANSFERENCIA A FAVOR DE LIMPIEZAS JACA", "RECIBO LIMPIEZAS JACA SL Nº 00%d" % i][i % 2])
        self.assertIn("limpiezas jaca", self.found())

    def months_of_history(self):
        for month in ("2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
            self.add(f"{month}-15", -5, "OTHER THING")  # the other things you buy: gives six months of history

    def test_five_of_six_months_is_enough_four_is_not(self):
        self.months_of_history()
        self.cleaning(CLEANING[:3] + CLEANING[4:])  # March missing
        self.assertIn("limpiezas jaca", self.found())
        self.db.conn.execute("DELETE FROM transactions WHERE description LIKE '%LIMPIEZAS%'")
        for day, amount in (CLEANING[0], CLEANING[1], CLEANING[4], CLEANING[5]):  # March and February missing
            self.add(day, amount, "RECIBO LIMPIEZAS JACA SL")
        self.assertNotIn("limpiezas jaca", self.found())

    def test_shops_visited_often_are_not_bills(self):
        for month in ("2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
            for day in (2, 6, 9, 13, 18, 22, 27):
                self.add(f"{month}-{day:02d}", -20 - day, "MERCADONA MADRID")
        self.cleaning()
        keys = self.found()
        self.assertNotIn("mercadona madrid", keys)
        self.assertIn("limpiezas jaca", keys)
        why = recurring.explain(self.db, "mercadona", TODAY)[0]
        self.assertFalse(why["detected"])
        self.assertTrue(any("shopping" in r for r in why["presence_reasons"]))

    def test_a_shop_you_visit_monthly_is_not_a_bill_unless_it_has_a_rhythm(self):
        for day, amount in CLEANING:
            self.add(day, amount, "PANADERIA PEPE")
        self.db.conn.execute("UPDATE transactions SET category='Groceries'")
        self.assertNotIn("panaderia pepe", self.found())
        why = recurring.explain(self.db, "panaderia", TODAY)[0]
        self.assertTrue(any("discretionary" in r for r in why["presence_reasons"]))
        self.db.conn.execute("UPDATE transactions SET category='Home & furniture'")
        self.assertIn("panaderia pepe", self.found())  # the same pattern in a bill-like category is found

    def test_needs_enough_history(self):
        self.cleaning(CLEANING[-3:])  # only three months of data
        self.assertEqual(self.found(), {})

    def test_every_two_months_is_found_by_cadence(self):
        for day in ("2025-07-10", "2025-09-12", "2025-11-09", "2026-01-11", "2026-03-10", "2026-05-12"):
            self.add(day, -60, "AGUA POTABLE SA")
        item = self.found()["agua potable"]
        self.assertEqual((item["kind"], item["cadence"]), ("cadence", "every two months"))
        self.assertEqual(item["monthly_equivalent"], 30.0)

    def test_drifting_monthly_dates_within_a_week_are_still_monthly(self):
        for day in ("2026-01-02", "2026-02-07", "2026-03-04", "2026-04-09", "2026-05-03"):
            self.add(day, -40, "GIMNASIO CLUB")
        self.assertEqual(self.found()["gimnasio club"]["cadence"], "monthly")

    def test_rejecting_and_restoring(self):
        self.cleaning()
        recurring.ignore(self.db, "limpiezas jaca", "out")
        self.assertNotIn("limpiezas jaca", self.found())
        self.assertEqual(recurring.summary(self.db, TODAY)["hidden"], [dict(key="limpiezas jaca", direction="out")])
        self.assertEqual(recurring.summary(self.db, TODAY)["monthly_costs"], 0)
        self.assertTrue(recurring.explain(self.db, "limpiezas", TODAY)[0]["hidden"])
        recurring.restore(self.db, "limpiezas jaca", "out")
        self.assertIn("limpiezas jaca", self.found())
        with self.assertRaises(ValueError):
            recurring.ignore(self.db, "", "out")
        with self.assertRaises(ValueError):
            recurring.ignore(self.db, "x", "sideways")

    def test_irregular_bill_is_not_called_ended_after_a_long_gap_between_dates(self):
        self.cleaning()
        late = date(2026, 6, 25)  # 52 days after the last invoice (4 May): still current for irregular dates
        item = {i["key"]: i for i in recurring.detect(self.db, late)}["limpiezas jaca"]
        self.assertTrue(item["active"])
        very_late = {i["key"]: i for i in recurring.detect(self.db, date(2026, 8, 15))}
        self.assertFalse(very_late.get("limpiezas jaca", {"active": False})["active"])

    def test_costs_total_uses_the_real_monthly_average(self):
        self.cleaning()
        self.assertEqual(recurring.summary(self.db, TODAY)["monthly_costs"], round(650 / 6, 2))

    def test_explain_for_unknown_text(self):
        self.assertEqual(recurring.explain(self.db, "nothing", TODAY), [])


class ApiTests(IsolatedHome):
    def test_ignore_and_restore_endpoints(self):
        db = Database("pw")
        from finances.sync import run_sync
        run_sync(db, {}, ["demo"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, {}))
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
            items = call("/api/recurring")[1]["items"]
            target = next(i for i in items if i["direction"] == "out")
            self.assertEqual(call("/api/recurring/ignore", {"key": target["key"], "direction": "out"})[0], 200)
            after = call("/api/recurring")[1]
            self.assertNotIn(target["key"], [i["key"] for i in after["items"] if i["direction"] == "out"])
            self.assertEqual(after["hidden"], [dict(key=target["key"], direction="out")])
            call("/api/recurring/restore", {"key": target["key"], "direction": "out"})
            self.assertEqual(call("/api/recurring")[1]["hidden"], [])
            self.assertEqual(call("/api/recurring/ignore", {"key": "", "direction": "out"})[0], 400)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
