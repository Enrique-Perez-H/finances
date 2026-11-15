import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize, recurring
from finances.db import Database
from finances.server import make_handler

TODAY = date(2026, 6, 15)
MONTHS = ("2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05")
NAMES = ["LIMPIEZAS JACA SL", "TRANSF A FAVOR DE LIMPIEZAS J", "RECIBO LIMPIEZA JACA", "L. JACA SERVICIOS",
         "LIMPIEZAS JACA", "PAGO LIMPIEZAS JACA SL"]  # the payee never reads the same way twice
AMOUNTS = [-90, -130, -70, -150, -100, -110]


class ManualTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        self.n = 0
        for month in MONTHS:  # six months of other life, so there is history
            self.add(f"{month}-15", -5, "OTHER THING")
        self.ids = [self.add(f"{m}-{3 + 4 * i:02d}", a, n)
                    for i, (m, a, n) in enumerate(zip(MONTHS, AMOUNTS, NAMES))]

    def add(self, day, amount, desc):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id="a", date=day, amount=amount,
                                        description=desc, category="uncategorized"))
        return f"t{self.n}"

    def items(self):
        return {i["key"]: i for i in recurring.detect(self.db, TODAY)}

    def test_picking_entries_with_different_names_makes_one_recurring_cost(self):
        gid = recurring.mark(self.db, self.ids, name="Home cleaning")
        item = self.items()[f"manual:{gid}"]
        self.assertEqual((item["label"], item["kind"], item["direction"], item["cadence"]),
                         ("Home cleaning", "manual", "out", "monthly"))
        self.assertEqual((item["occurrences"], item["amount_min"], item["amount_max"]), (6, 70.0, 150.0))
        self.assertEqual(item["monthly_equivalent"], round(650 / 6, 2))
        self.assertTrue(item["active"])
        self.assertEqual(len(item["entries"]), 6)

    def test_picked_entries_are_not_also_guessed_again(self):
        recurring.mark(self.db, self.ids, name="Home cleaning")
        manual = [k for k in self.items() if k.startswith("manual:")]
        self.assertEqual(len(manual), 1)
        self.assertFalse(any("limpiezas" in k for k in self.items()))  # no duplicate automatic item

    def test_name_variants_pull_in_later_entries_automatically(self):
        gid = recurring.mark(self.db, self.ids[:3], name="Home cleaning", pattern="limpiezas jaca")
        self.assertGreater(self.items()[f"manual:{gid}"]["occurrences"], 3)  # same-name entries joined
        self.add("2026-06-02", -95, "SERVICIOS LIMPIEZAS JACA S.L.")
        recurring.add_pattern(self.db, gid, "l. jaca servicios")
        n_before = self.items()[f"manual:{gid}"]["occurrences"]
        self.add("2026-06-04", -80, "L. JACA SERVICIOS 0612")
        self.assertEqual(self.items()[f"manual:{gid}"]["occurrences"], n_before + 1)
        # a receipt with the same name does not join a cost
        self.add("2026-06-05", 80, "LIMPIEZAS JACA SL devolucion")
        self.assertEqual(self.items()[f"manual:{gid}"]["occurrences"], n_before + 1)

    def test_add_to_an_existing_item_and_remove_things(self):
        gid = recurring.mark(self.db, self.ids[:2], name="Home cleaning")
        recurring.mark(self.db, self.ids[2:], group_id=gid)
        self.assertEqual(self.items()[f"manual:{gid}"]["occurrences"], 6)
        recurring.unlink(self.db, gid, self.ids[0])
        self.assertEqual(self.items()[f"manual:{gid}"]["occurrences"], 5)
        recurring.add_pattern(self.db, gid, "limpiezas")
        recurring.remove_pattern(self.db, gid, "limpiezas")
        recurring.update_manual(self.db, gid, name="Cleaning", cadence="every two months")
        item = self.items()[f"manual:{gid}"]
        self.assertEqual((item["label"], item["cadence"]), ("Cleaning", "every two months"))
        recurring.delete_manual(self.db, gid)
        self.assertEqual([k for k in self.items() if k.startswith("manual:")], [])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM recurring_manual_entries").fetchone()[0], 0)

    def test_receipts_can_be_recurring_income(self):
        a = self.add("2026-04-01", 500, "RENT FROM X"); b = self.add("2026-05-02", 520, "ALQ CASA PEREZ")
        gid = recurring.mark(self.db, [a, b], name="Tenant")
        item = self.items()[f"manual:{gid}"]
        self.assertEqual(item["direction"], "in")
        with self.assertRaises(ValueError):  # a payment cannot join an income item
            recurring.mark(self.db, [self.ids[0]], group_id=gid)

    def test_costs_total_includes_marked_items(self):
        recurring.mark(self.db, self.ids, name="Home cleaning")
        # the marked cleaning (650 over 6 months) plus the 5 euros a month "OTHER THING" detected by itself
        self.assertEqual(recurring.summary(self.db, TODAY)["monthly_costs"], round(650 / 6 + 5, 2))

    def test_average_starts_when_the_cost_started(self):
        gid = recurring.mark(self.db, self.ids[-2:], name="Newer")  # Apr and May only
        self.assertEqual(self.items()[f"manual:{gid}"]["monthly_equivalent"], round((100 + 110) / 2, 2))

    def test_paired_and_internal_entries_are_not_counted(self):
        a = self.add("2026-05-20", -60, "LIMPIEZAS JACA SL"); b = self.add("2026-05-22", 60, "BIZUM X")
        categorize.categorize_all(self.db)  # a and b cancel out as a reimbursement
        gid = recurring.mark(self.db, [a], name="Home cleaning")
        self.assertEqual(self.items()[f"manual:{gid}"]["occurrences"], 0)

    def test_validation(self):
        for kwargs in (dict(tx_ids=self.ids, name=""), dict(tx_ids=self.ids, name="x" * 61),
                       dict(tx_ids=self.ids, name="ok", cadence="daily"), dict(tx_ids=["ghost"], name="ok"),
                       dict(tx_ids=[], name="ok"), dict(tx_ids=self.ids, name="ok", pattern="transferencia"),
                       dict(tx_ids=self.ids, name="ok", pattern="ab"), dict(tx_ids=self.ids, group_id=99)):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                recurring.mark(self.db, **kwargs)
        mixed = self.add("2026-06-01", 10, "SOMETHING IN")
        with self.assertRaises(ValueError):
            recurring.mark(self.db, [self.ids[0], mixed], name="ok")
        with self.assertRaises(ValueError):
            recurring.update_manual(self.db, 99, name="x")

    def test_a_pattern_alone_can_start_an_item(self):
        gid = recurring.mark(self.db, [], name="Cleaning", pattern="limpiezas jaca")
        self.assertGreaterEqual(self.items()[f"manual:{gid}"]["occurrences"], 3)
        self.add("2026-05-25", 700, "ALQUILER GARAJE PEREZ")
        inc = recurring.mark(self.db, [], name="Garage rent", pattern="alquiler garaje", direction="in")
        self.assertEqual(self.items()[f"manual:{inc}"]["direction"], "in")
        self.assertEqual(self.items()[f"manual:{inc}"]["occurrences"], 1)


class AverageMonthTests(IsolatedHome):
    """Marked recurring costs count at their monthly equivalent in the average month."""

    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        self.n = 0
        for month in ("2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
                      "2026-03", "2026-04", "2026-05"):
            self.add(f"{month}-15", -100, "GROCERY STORE", "Groceries")  # 100 a month, nothing else

    def add(self, day, amount, desc, category):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id="a", date=day, amount=amount,
                                        description=desc, category=category, cat_source="manual"))
        return f"t{self.n}"

    def avg(self, months=12):
        return analysis.average_month(self.db, months, today=TODAY)

    def by(self, result):
        return {c["category"]: c for c in result["categories"]}

    def test_a_yearly_bill_is_a_twelfth_every_month(self):
        bill = self.add("2025-09-20", -600, "SEGURO COCHE", "Insurance")
        unmarked = self.avg(12)
        self.assertEqual(self.by(unmarked)["Insurance"]["average"], round(600 / 11, 2))  # lumpy: depends on the window
        short = self.avg(6)  # and a 6-month window misses the payment entirely
        self.assertNotIn("Insurance", self.by(short))
        recurring.mark(self.db, [bill], name="Car insurance", cadence="yearly")
        marked = self.avg(6)
        self.assertEqual(self.by(marked)["Insurance"]["average"], 50.0)
        self.assertEqual(self.by(marked)["Insurance"]["recurring_part"], 50.0)
        self.assertEqual(marked["recurring"], [dict(name="Car insurance", monthly=50.0, cadence="yearly", category="Insurance")])
        self.assertEqual((marked["recurring_total"], marked["total"]), (50.0, 150.0))  # 100 groceries + 50

    def test_the_marked_entries_are_not_counted_twice(self):
        bill = self.add("2025-09-20", -600, "SEGURO COCHE", "Insurance")
        recurring.mark(self.db, [bill], name="Car insurance", cadence="yearly")
        self.assertEqual(self.by(self.avg(12))["Insurance"]["total"], 550.0)  # 11 months x 50, not 600 + 550
        self.assertAlmostEqual(self.avg(12)["total"], 100 * 11 / 11 + 50, places=2)

    def test_quarterly_and_monthly_items(self):
        q = self.add("2026-02-10", -90, "COMUNIDAD", "Housing")
        recurring.mark(self.db, [q], name="Community fees", cadence="quarterly")
        self.assertEqual(self.by(self.avg(12))["Housing"]["average"], 30.0)  # 90 every three months
        entries = [self.add(f"{m}-03", -40 - i, "CLEANING", "Home & furniture")
                   for i, m in enumerate(("2026-03", "2026-04", "2026-05"))]
        recurring.mark(self.db, entries, name="Cleaning")  # monthly, three months so far
        cleaning = [r for r in self.avg(12)["recurring"] if r["name"] == "Cleaning"][0]
        self.assertEqual(cleaning["monthly"], round((40 + 41 + 42) / 3, 2))

    def test_the_recurring_overview_uses_the_same_monthly_equivalent(self):
        bill = self.add("2025-09-20", -600, "SEGURO COCHE", "Insurance")
        gid = recurring.mark(self.db, [bill], name="Car insurance", cadence="yearly")
        item = {i["key"]: i for i in recurring.detect(self.db, TODAY)}[f"manual:{gid}"]
        self.assertEqual(item["monthly_equivalent"], 50.0)

    def test_income_items_and_empty_items_do_not_change_the_average(self):
        pay = self.add("2026-03-01", 300, "RENT FROM X", "Other income")
        recurring.mark(self.db, [pay], name="Tenant")
        recurring.mark(self.db, [], name="Nothing yet", pattern="zzzzzz")
        result = self.avg(12)
        self.assertEqual((result["recurring"], result["total"]), ([], 100.0))

    def test_without_recurring_items_nothing_changes(self):
        result = self.avg(12)
        self.assertEqual((result["total"], result["recurring_total"], result["recurring"]), (100.0, 0.0, []))
        self.assertEqual(self.by(result)["Groceries"]["recurring_part"], 0.0)


class ApiTests(IsolatedHome):
    def test_endpoints(self):
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
            ids = [t["id"] for t in call("/api/entries?q=carrefour&limit=3")[1]["items"]]
            code, res = call("/api/recurring/manual", {"tx_ids": ids, "name": "Corner shop", "cadence": "monthly"})
            self.assertEqual(code, 200)
            gid = res["group_id"]
            items = call("/api/recurring")[1]["items"]
            self.assertTrue(any(i["key"] == f"manual:{gid}" for i in items))
            self.assertEqual(call("/api/recurring/manual/pattern", {"group_id": gid, "pattern": "carrefour express"})[0], 200)
            self.assertEqual(call("/api/recurring/manual/pattern", {"group_id": gid, "pattern": "carrefour express", "remove": True})[0], 200)
            self.assertEqual(call("/api/recurring/manual/update", {"group_id": gid, "name": "Shop", "cadence": "quarterly"})[0], 200)
            self.assertEqual(call("/api/recurring/manual/unlink", {"group_id": gid, "tx_id": ids[0]})[0], 200)
            self.assertEqual(call("/api/recurring/manual", {"tx_ids": ids, "name": ""})[0], 400)
            self.assertEqual(call("/api/recurring/manual/pattern", {"pattern": "abc"})[0], 400)
            self.assertEqual(call("/api/recurring/manual/delete", {"group_id": gid})[0], 200)
            self.assertFalse(any(i["key"] == f"manual:{gid}" for i in call("/api/recurring")[1]["items"]))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
