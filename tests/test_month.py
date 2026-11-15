import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize, recurring, tricount
from finances.db import Database
from finances.server import make_handler

TODAY = date(2026, 11, 20)


class MonthTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        self.n = 0
        self.rent = self.add("2026-10-01", -750, "ALQUILER", "Housing")
        self.daycare = self.add("2026-10-05", -400, "GUARDERIA", "Kids")
        self.clean1 = self.add("2026-10-12", -80, "LIMPIEZAS JACA SL", "Home & furniture")
        self.mystery = self.add("2026-10-14", -33, "PAGO XYZ", "uncategorized")
        self.pay = self.add("2026-10-28", 2000, "NOMINA", "Salary")
        self.add("2026-10-29", -5000, "A TRADE REPUBLIC", categorize.INTERNAL)
        self.add("2026-11-02", -60, "NOVEMBER THING", "Other")
        tricount.set_share(self.db, [self.daycare], 0.5)
        recurring.mark(self.db, [self.rent], name="Rent", cadence="monthly")
        recurring.mark(self.db, [self.clean1], name="Cleaning", pattern="limpiezas jaca")
        self.add("2026-11-10", -90, "RECIBO LIMPIEZAS JACA SL", "Home & furniture")  # joins by name

    def add(self, day, amount, desc, category):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id="a", date=day, amount=amount,
                                        description=desc, category=category, cat_source="manual"))
        return f"t{self.n}"

    def entries(self, **kw):
        return {e["id"]: e for e in analysis.entries(self.db, since="2026-10-01", until="2026-10-31", limit=100, **kw)["items"]}

    def test_entries_know_their_account_share_and_recurring_item(self):
        e = self.entries()
        self.assertEqual(e[self.rent]["recurring"], dict(group_id=1, name="Rent", explicit=True))
        self.assertIsNone(e[self.daycare]["recurring"])
        self.assertEqual((e[self.daycare]["share"], e[self.daycare]["amount"], e[self.daycare]["raw_amount"]), (0.5, -200.0, -400.0))
        self.assertEqual(e[self.rent]["account_id"], "a")

    def test_entries_that_join_by_name_are_marked_as_not_picked(self):
        november = {e["id"]: e for e in analysis.entries(self.db, since="2026-11-01", until="2026-11-30", limit=100)["items"]}
        joined = [e for e in november.values() if e["recurring"] and e["recurring"]["name"] == "Cleaning"]
        self.assertEqual(len(joined), 1)
        self.assertFalse(joined[0]["recurring"]["explicit"])

    def test_flag_filters(self):
        self.assertEqual(set(self.entries(flag="review")), {self.mystery})
        self.assertEqual(set(self.entries(flag="shared")), {self.daycare})
        self.assertEqual(set(self.entries(flag="recurring")), {self.rent, self.clean1})
        self.assertEqual(len(self.entries()), 7 - 1)  # six entries in October

    def test_month_overview_numbers(self):
        m = analysis.month_overview(self.db, "2026-10", TODAY)
        self.assertEqual((m["income"], m["spending"]), (2000.0, 750 + 200 + 80 + 33))
        self.assertEqual(m["saved"], round(2000 - 1063, 2))
        self.assertEqual(m["removed_by_sharing"], 200.0)  # half of the daycare no longer counts
        self.assertEqual((m["shared"], m["to_review"], m["recurring"], m["entries"]), (1, 1, 2, 6))
        self.assertFalse(m["partial"])
        self.assertEqual(m["months"], ["2026-11", "2026-10"])

    def test_the_overview_follows_edits(self):
        tricount.set_share(self.db, [self.mystery], 0.5)
        m = analysis.month_overview(self.db, "2026-10", TODAY)
        self.assertEqual((m["spending"], m["removed_by_sharing"], m["shared"]), (1063 - 16.5, 216.5, 2))
        tricount.set_share(self.db, [self.daycare, self.mystery], 1)
        self.assertEqual(analysis.month_overview(self.db, "2026-10", TODAY)["removed_by_sharing"], 0.0)

    def test_a_month_with_nothing_and_a_bad_month(self):
        m = analysis.month_overview(self.db, "2020-01", TODAY)
        self.assertEqual((m["income"], m["spending"], m["entries"], m["removed_by_sharing"]), (0.0, 0.0, 0, 0.0))
        for bad in ("2026", "2026-5", "abcd-ef"):
            with self.assertRaises(ValueError):
                analysis.month_overview(self.db, bad)
        self.assertEqual(analysis.month_overview(self.db, "", TODAY)["month"], "2026-11")  # empty = the latest month

    def test_current_month_is_flagged_partial(self):
        self.assertTrue(analysis.month_overview(self.db, "2026-11", TODAY)["partial"])

    def test_endpoints(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, {}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            def get(path):
                try:
                    with urllib.request.urlopen(base + path) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            code, m = get("/api/month?month=2026-10")
            self.assertEqual((code, m["removed_by_sharing"]), (200, 200.0))
            self.assertEqual(get("/api/month?month=nope")[0], 400)
            code, res = get("/api/entries?since=2026-10-01&until=2026-10-31&flag=shared")
            self.assertEqual([e["id"] for e in res["items"]], [self.daycare])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
