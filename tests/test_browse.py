import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize
from finances.db import Database
from finances.server import make_handler


class BrowseTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        rows = [("1", "2026-01-05", -30, "CAFÉ LA ESQUINA", "Restaurants & bars"),
                ("2", "2026-02-10", -90, "MERCADONA", "Groceries"),
                ("3", "2026-02-11", -15, "mercadona express", "Groceries"),
                ("4", "2026-03-01", 2000, "NOMINA", "Salary"),
                ("5", "2026-03-02", -500, "PAGO RARO", "uncategorized"),
                ("6", "2026-03-03", -60, "RESTAURANTE X", "Restaurants & bars"),
                ("7", "2026-03-05", 60, "BIZUM DE LUIS", "Transfers received")]
        for id, day, amount, desc, cat in rows:
            self.db.upsert_transaction(dict(id=id, account_id="a", date=day, amount=amount,
                                            description=desc, category=cat, cat_source="manual"))
        categorize.categorize_all(self.db)  # the restaurant 60 and the Bizum 60 cancel out

    def ids(self, **kw):
        return [t["id"] for t in analysis.entries(self.db, **kw)["items"]]

    def test_filters_sorting_and_totals(self):
        self.assertEqual(self.ids(category="Groceries"), ["3", "2"])                  # newest first
        self.assertEqual(self.ids(category="Groceries", sort="amount"), ["2", "3"])  # biggest first
        self.assertEqual(self.ids(category="uncategorized"), ["5"])
        self.assertEqual(len(self.ids(category="*")), 5)  # the restaurant/Bizum pair is left out
        res = analysis.entries(self.db, category="Groceries")
        self.assertEqual((res["count"], res["total_out"], res["total_in"]), (2, 105.0, 0.0))

    def test_dates_and_accent_insensitive_search(self):
        self.assertEqual(self.ids(since="2026-02-01", until="2026-02-28"), ["3", "2"])
        self.assertEqual(self.ids(q="cafe"), ["1"])        # CAFÉ found with plain "cafe"
        self.assertEqual(self.ids(q="MERCADONA", category="*"), ["3", "2"])
        self.assertEqual(self.ids(q="nothing here"), [])

    def test_pagination(self):
        first = analysis.entries(self.db, limit=2)
        self.assertEqual((len(first["items"]), first["has_more"]), (2, True))
        rest = analysis.entries(self.db, offset=2, limit=50)
        self.assertEqual((len(rest["items"]), rest["has_more"]), (3, False))
        self.assertEqual(len(analysis.entries(self.db, limit=10000)["items"]), 5)  # capped, not an error

    def test_paired_entries_are_not_listed(self):
        listed = self.ids()
        self.assertNotIn("6", listed)
        self.assertNotIn("7", listed)

    def test_items_carry_rule_wording_for_move_all(self):
        item = analysis.entries(self.db, category="Groceries")["items"][0]
        self.assertEqual((item["key"], item["pattern"]), ("mercadona express", "mercadona"))

    def test_bulk_move_is_manual_and_validated(self):
        self.assertEqual(categorize.set_manual_many(self.db, ["2", "3"], "Home & furniture"), 2)
        categorize.add_rule(self.db, "mercadona", "Groceries")  # rules do not undo a hand choice
        cats = {r[0] for r in self.db.conn.execute("SELECT category FROM transactions WHERE id IN ('2','3')")}
        self.assertEqual(cats, {"Home & furniture"})
        for bad in (([], "Other"), (["2"], ""), (["2"], "uncategorized"), (["2"] * 501, "Other")):
            with self.assertRaises(ValueError):
                categorize.set_manual_many(self.db, *bad)


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
            code, res = call("/api/entries?category=Groceries&sort=amount&limit=3")
            self.assertEqual(code, 200)
            self.assertEqual(len(res["items"]), 3)
            self.assertEqual(call("/api/entries?offset=abc")[0], 400)
            ids = [t["id"] for t in res["items"]][:2]
            code, moved = call("/api/categorize/bulk", {"tx_ids": ids, "category": "Leisure"})
            self.assertEqual((code, moved["moved"]), (200, 2))
            self.assertEqual(call("/api/categorize/bulk", {"tx_ids": "x", "category": "Leisure"})[0], 400)
            self.assertEqual(call("/api/categorize/bulk", {"tx_ids": [], "category": "Leisure"})[0], 400)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
