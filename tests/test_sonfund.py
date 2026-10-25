import json
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import sonfund
from finances.db import Database
from finances.server import make_handler

TODAY = date(2026, 3, 20)


class SonFundTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        # Account: 10,000 deposited in January, interest paid on 1 Feb (20) and 1 Mar (21).
        self.db.upsert_account(dict(id="tr", provider="t", name="TR cash", kind="investment",
                                    currency="EUR", balance=10041, updated_at="t"))
        self.tx("d", "2026-01-10", 10000, "Deposit", "Internal transfer")
        self.tx("i1", "2026-02-01", 20, "TR interest Zinsen", sonfund.INTEREST)
        self.tx("i2", "2026-03-01", 21, "TR interest Zinsen", sonfund.INTEREST)

    def tx(self, id, day, amount, desc, category):
        self.db.upsert_transaction(dict(id=id, account_id="tr", date=day, amount=amount,
                                        description=desc, category=category, cat_source="connector"))

    def configure(self, **kw):
        body = dict(name="Leo", account_id="tr", start_date="2026-01-15", initial=2500, monthly=0, years=1)
        return sonfund.save_config(self.db, {**body, **kw})

    def test_share_of_each_payment_compounds(self):
        self.configure()
        sim = sonfund.simulate(self.db, TODAY)
        feb, mar = sim["rows"]
        self.assertEqual((feb["month"], feb["account_balance"], feb["share"]), ("2026-02", 10000.0, 0.25))
        self.assertEqual((feb["interest"], feb["son_interest"], feb["son_balance"]), (20.0, 5.0, 2505.0))
        self.assertEqual((mar["account_balance"], mar["share"]), (10020.0, 0.25))
        self.assertEqual((mar["son_interest"], mar["son_balance"]), (5.25, 2510.25))  # earns on the interest too
        self.assertTrue(mar["partial"])
        self.assertEqual(sim["son_balance"], 2510.25)
        self.assertEqual(sim["interest_total"], 10.25)
        self.assertAlmostEqual(sim["share_now"], 2510.25 / 10041, places=4)

    def test_deposits_count_from_their_month_after_the_share_is_taken(self):
        self.configure()
        sonfund.add_deposit(self.db, {"date": "2026-02-20", "amount": 500, "note": "gift"})
        sonfund.add_deposit(self.db, {"date": "2025-12-01", "amount": 999})  # before the start: ignored
        feb = sonfund.simulate(self.db, TODAY)["rows"][0]
        self.assertEqual((feb["son_interest"], feb["deposits"], feb["son_balance"]), (5.0, 500.0, 3005.0))
        mar = sonfund.simulate(self.db, TODAY)["rows"][1]
        self.assertAlmostEqual(mar["share"], 3005 / 10020, places=4)

    def test_dividends_are_not_shared_when_interest_is_marked(self):
        self.tx("div", "2026-03-05", 50, "Dividende Apple", sonfund.INTEREST)
        self.configure()
        mar = sonfund.simulate(self.db, TODAY)["rows"][1]
        self.assertEqual(mar["interest"], 21.0)  # only the marked interest payout

    def test_unmarked_interest_is_used_when_nothing_is_marked(self):
        self.db.conn.execute("UPDATE transactions SET description='Interest' WHERE id IN ('i1','i2')")
        self.configure()
        self.assertEqual(sonfund.simulate(self.db, TODAY)["rows"][0]["interest"], 20.0)

    def test_share_never_exceeds_the_account(self):
        self.configure(initial=50000)
        self.assertEqual(sonfund.simulate(self.db, TODAY)["rows"][0]["share"], 1.0)

    def test_projection_uses_given_rate_and_monthly_deposits(self):
        self.configure(annual_rate=12, monthly=100, years=1)
        sim = sonfund.simulate(self.db, TODAY)
        self.assertEqual(len(sim["projection"]), 12)
        self.assertEqual(sim["projection"][0]["month"], "2026-04")
        r = 1.12 ** (1 / 12) - 1
        value = sim["son_balance"]
        for _ in range(12):
            value = value * (1 + r) + 100
        self.assertAlmostEqual(sim["projected_end"], value, delta=0.05)
        self.assertEqual(sim["rate_used"], 12.0)

    def test_rate_is_estimated_from_real_payments(self):
        self.configure(annual_rate=None)
        sim = sonfund.simulate(self.db, date(2026, 4, 10))  # Feb and Mar are now completed months
        monthly = (20 / 10000 + 21 / 10020) / 2
        self.assertAlmostEqual(sim["rate_estimated"], ((1 + monthly) ** 12 - 1) * 100, places=1)
        self.assertEqual(sim["rate_used"], sim["rate_estimated"])
        self.assertTrue(sim["projection"])

    def test_no_data_cases_are_explained_not_crashing(self):
        self.assertEqual(sonfund.simulate(self.db, TODAY), {"configured": False})
        self.configure(account_id="tr")
        self.db.conn.execute("DELETE FROM transactions")
        sim = sonfund.simulate(self.db, TODAY)
        self.assertTrue(sim["notes"])
        self.assertEqual(sim["rows"][0]["share"], None)  # balance history unknown

    def test_validation(self):
        for bad in ({"name": ""}, {"account_id": "ghost"}, {"start_date": "soon"}, {"initial": -1},
                    {"annual_rate": 500}, {"years": 0}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.configure(**bad)
        with self.assertRaises(ValueError):
            sonfund.add_deposit(self.db, {"date": "2026-01-01", "amount": 0})

    def test_survives_encrypted_roundtrip_and_delete(self):
        self.configure()
        dep = sonfund.add_deposit(self.db, {"date": "2026-02-02", "amount": 10})
        self.db.save()
        again = Database("pw")
        self.assertEqual(sonfund.get_config(again)["name"], "Leo")
        sonfund.delete_deposit(again, dep["id"])
        self.assertEqual(sonfund.payload(again)["deposits"], [])


class ApiTests(IsolatedHome):
    def test_endpoints(self):
        db = Database("pw")
        db.upsert_account(dict(id="tr", provider="t", name="TR", kind="cash", currency="EUR",
                               balance=1000, updated_at="t"))
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
            self.assertEqual(call("/api/sonfund")[1]["configured"], False)
            self.assertEqual(call("/api/sonfund", {"name": "", "account_id": "tr"})[0], 400)
            code, cfg = call("/api/sonfund", {"name": "Leo", "account_id": "tr", "start_date": "2026-01-01",
                                              "initial": 100, "annual_rate": 3, "years": 2})
            self.assertEqual(code, 200)
            code, dep = call("/api/sonfund/deposit", {"date": "2026-02-01", "amount": 50})
            self.assertEqual(code, 200)
            data = call("/api/sonfund")[1]
            self.assertEqual((data["configured"], len(data["deposits"]), len(data["accounts"])), (True, 1, 1))
            self.assertEqual(len(data["projection"]), 24)
            self.assertEqual(call("/api/sonfund/deposit/delete", {"id": dep["id"]})[0], 200)
            self.assertEqual(call("/api/sonfund/deposit", {"date": "x", "amount": 5})[0], 400)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
