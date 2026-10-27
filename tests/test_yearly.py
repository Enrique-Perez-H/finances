import csv
import io
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import categorize, yearly
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync


class YearlyTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        for id, name in (("a", "Santander"), ("tr", "Trade Republic Cash")):
            self.db.upsert_account(dict(id=id, provider="t", name=name, kind="cash", currency="EUR",
                                        balance=0, updated_at="t"))
        self.n = 0

    def add(self, day, amount, desc, category, account="a"):
        self.n += 1
        self.db.upsert_transaction(dict(id=f"t{self.n}", account_id=account, date=day, amount=amount,
                                        description=desc, category=category, cat_source="manual"))

    def seed(self):
        self.add("2025-01-28", 2000, "NOMINA ACME", "Salary")
        self.add("2025-02-28", 2000, "NOMINA ACME", "Salary")
        self.add("2025-03-05", 2200, "PENSION TESORERIA", "Pension & benefits")
        self.add("2025-02-01", 12.5, "TR interest Zinsen", "Interest & dividends", "tr")
        self.add("2025-03-01", 13.0, "TR interest Zinsen", "Interest & dividends", "tr")
        self.add("2025-03-10", 40.0, "Dividende Apple", "Interest & dividends", "tr")
        self.add("2025-01-03", -750, "ALQUILER", "Housing")
        self.add("2025-02-09", -300, "HACIENDA IRPF", "Taxes & fees")
        self.add("2025-03-09", -120, "TGSS COTIZACION", "Taxes & fees")
        self.add("2025-03-12", -5000, "A TRADE REPUBLIC", categorize.INTERNAL)
        self.add("2025-03-12", 5000, "DE SANTANDER", categorize.INTERNAL, "tr")
        self.add("2025-04-01", -231000, "INMOBILIARIA", categorize.CAPITAL)
        self.add("2025-05-01", -60, "RESTAURANTE", "Restaurants & bars")
        self.add("2025-05-03", 60, "BIZUM DE LUIS", "Transfers received")
        self.add("2026-01-02", -10, "OTHER YEAR", "Groceries")
        categorize.categorize_all(self.db)

    def test_summary_counts_like_the_monthly_views(self):
        self.seed()
        s = yearly.summary(self.db, "2025")
        self.assertEqual(s["income"]["total"], 2000 + 2000 + 2200 + 12.5 + 13 + 40)
        self.assertEqual(s["spending"]["total"], 750 + 300 + 120)  # restaurant/Bizum pair cancels out
        self.assertEqual(s["saved"], round(s["income"]["total"] - s["spending"]["total"], 2))
        cats = {c["category"]: c["total"] for c in s["income"]["by_category"]}
        self.assertEqual(cats["Salary"], 4000.0)
        self.assertEqual(cats["Pension & benefits"], 2200.0)
        self.assertNotIn("Internal transfer", cats)
        self.assertEqual(s["income"]["by_category"][0]["category"], "Salary")  # biggest first
        self.assertEqual(s["years"], ["2026", "2025"])

    def test_interest_is_split_from_dividends_per_account(self):
        self.seed()
        i = yearly.summary(self.db, "2025")["interest"]
        self.assertEqual((i["interest"], i["other"], i["total"]), (25.5, 40.0, 65.5))
        self.assertEqual(i["accounts"], [dict(account="Trade Republic Cash", interest=25.5, other=40.0)])

    def test_taxes_and_not_counted(self):
        self.seed()
        s = yearly.summary(self.db, "2025")
        self.assertEqual(s["taxes_paid"]["total"], 420.0)
        self.assertEqual(s["taxes_paid"]["top"][0]["amount"], 300.0)
        nc = s["not_counted"]
        self.assertEqual((nc["internal_out"], nc["internal_in"]), (5000.0, 5000.0))
        self.assertEqual((nc["capital_out"], nc["reimbursed_spent"], nc["reimbursed_received"], nc["reimbursed_pairs"]),
                         (231000.0, 60.0, 60.0, 1))

    def test_months_and_sources(self):
        self.seed()
        s = yearly.summary(self.db, "2025")
        self.assertEqual(len(s["monthly"]), 12)
        feb = next(m for m in s["monthly"] if m["month"] == "2025-02")
        self.assertEqual((feb["income"], feb["spending"]), (2012.5, 300.0))
        self.assertEqual(s["income_sources"][0]["total"], 4000.0)

    def test_empty_year(self):
        s = yearly.summary(self.db, "2030")
        self.assertEqual((s["income"]["total"], s["savings_rate"]), (0, None))


class CsvTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="Santander", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))
        for i, (amount, desc, cat) in enumerate([(-1234.5, "CAFÉ; con punto y coma", "Restaurants & bars"),
                                                 (-5, "=HYPERLINK(\"http://x\")", "Other"),
                                                 (300, "TRANSFER IN", categorize.INTERNAL)]):
            self.db.upsert_transaction(dict(id=str(i), account_id="a", date="2025-03-0%d" % (i + 1), amount=amount,
                                            description=desc, category=cat, cat_source="manual"))
        categorize.categorize_all(self.db)

    def parse(self, data, delimiter):
        return list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=delimiter))

    def test_spanish_style_and_standard_style(self):
        es = yearly.export_entries(self.db, "2025", "es")
        self.assertTrue(es.startswith(b"\xef\xbb\xbf"))  # BOM so Excel reads the accents
        rows = self.parse(es, ";")
        self.assertEqual(rows[0][:5], ["Date", "Account", "Description", "Amount", "Category"])
        self.assertEqual(rows[1][2:4], ["CAFÉ; con punto y coma", "-1234,50"])  # semicolon quoted, decimal comma
        plain = self.parse(yearly.export_entries(self.db, "2025", "csv"), ",")
        self.assertEqual(plain[1][3], "-1234.50")

    def test_counted_column_and_reason(self):
        rows = self.parse(yearly.export_entries(self.db, "2025", "csv"), ",")
        self.assertEqual([(r[5], r[6]) for r in rows[1:]],
                         [("yes", ""), ("yes", ""), ("no", "Internal transfer")])

    def test_formula_injection_is_defused(self):
        rows = self.parse(yearly.export_entries(self.db, "2025", "csv"), ",")
        self.assertTrue(rows[2][2].startswith("'="))

    def test_all_years_and_summary(self):
        self.assertEqual(len(self.parse(yearly.export_entries(self.db, "all", "csv"), ",")), 4)
        rows = self.parse(yearly.export_summary(self.db, "2025", "csv"), ",")
        self.assertEqual(rows[0], ["Section", "Item", "Amount"])
        self.assertIn(["Total", "Spending", "1239.50"], rows)


class ApiTests(IsolatedHome):
    def test_endpoints(self):
        db = Database("pw")
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
                        return r.status, r.headers, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.headers, e.read()
            code, _, body = call("/api/yearly")  # no year: the latest one with data
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["year"].isdigit())
            self.assertEqual(call("/api/yearly?year=abc")[0], 400)
            code, headers, data = call("/api/export", {"year": "all", "kind": "entries", "style": "es"})
            self.assertEqual(code, 200)
            self.assertIn("text/csv", headers["Content-Type"])
            self.assertIn("finances-all-entries.csv", headers["Content-Disposition"])
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(call("/api/export", {"year": "all", "kind": "summary"})[0], 400)
            self.assertEqual(call("/api/export", {"year": "x", "kind": "entries"})[0], 400)
            self.assertEqual(call("/api/export", {"year": "2025", "kind": "nope"})[0], 400)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
