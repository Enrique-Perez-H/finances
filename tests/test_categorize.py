import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome
from finances import analysis, categorize
from finances.categorize import INTERNAL, UNCATEGORIZED, classify, merchant_key, normalize
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync


class ClassifyTests(IsolatedHome):
    def test_normalize_and_key(self):
        self.assertEqual(normalize("Nómina  Octubre!"), "nomina octubre")
        self.assertEqual(merchant_key("COMPRA TARJ. 5540XXXX MERCADONA MADRID"), "mercadona madrid")

    def test_defaults_and_sign(self):
        self.assertEqual(classify("MERCADONA", -20, [])[0], "Groceries")
        self.assertEqual(classify("AMAZON EU", 12, [])[0], "Refunds")
        self.assertEqual(classify("Bizum de Ana", 15, [])[0], "Transfers received")
        self.assertEqual(classify("Bizum a Luis", -15, [])[0], "Other")
        self.assertEqual(classify("TRANSFERENCIA A TRADE REPUBLIC", -500, [])[0], INTERNAL)
        self.assertEqual(classify("Nómina ACME", 2000, [])[0], "Salary")

    def test_short_pattern_needs_whole_word(self):
        self.assertEqual(classify("MEDIA MARKT", -5, [])[0], UNCATEGORIZED)  # not "dia"
        self.assertEqual(classify("SUPERMERCADOS DIA", -5, [])[0], "Groceries")

    def test_specific_beats_generic(self):
        self.assertEqual(classify("UBER EATS", -9, [])[0], "Restaurants & bars")
        self.assertEqual(classify("UBER TRIP", -9, [])[0], "Transport")
        self.assertEqual(classify("AMAZON PRIME", -5, [])[0], "Subscriptions")

    def test_user_rule_beats_default(self):
        self.assertEqual(classify("MERCADONA", -20, [("mercadona", "Kids")]), ("Kids", "user"))


class MccAndTransferTests(IsolatedHome):
    def test_mcc_is_the_fallback_after_keywords(self):
        self.assertEqual(classify("PAGO XYZ SL", -20, [], "5411"), ("Groceries", "mcc"))
        self.assertEqual(classify("PAGO XYZ SL", -20, [], "5812"), ("Restaurants & bars", "mcc"))
        self.assertEqual(classify("PAGO XYZ SL", -20, [], "3500")[0], "Travel")
        self.assertEqual(classify("PAGO XYZ SL", -20, [], "0000"), (UNCATEGORIZED, ""))
        self.assertEqual(classify("MERCADONA", -20, [], "5812")[0], "Groceries")  # keyword wins
        self.assertEqual(classify("PAGO XYZ SL", -20, [("xyz", "Kids")], "5411"), ("Kids", "user"))

    def test_trade_republic_variants_are_internal(self):
        for text in ("Trade Republic Bank GmbH", "TRADEREPUBLIC", "ES TRBKDEBB transfer"):
            self.assertEqual(classify(text, -300, [])[0], INTERNAL, text)

    def test_mcc_stored_and_used_by_categorizer(self):
        db = Database("pw")
        db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                               balance=0, updated_at="t"))
        db.upsert_transaction(dict(id="1", account_id="a", date="2026-01-01", amount=-9,
                                   description="Transaction", category="uncategorized", mcc="5541"))
        categorize.categorize_all(db)
        self.assertEqual(db.conn.execute("SELECT category FROM transactions").fetchone()[0], "Transport")

    def test_old_database_gets_mcc_column(self):
        db = Database("pw")
        db.conn.execute("ALTER TABLE transactions DROP COLUMN mcc")
        db._migrate()
        self.assertIn("mcc", {r["name"] for r in db.conn.execute("PRAGMA table_info(transactions)")})


class RealWorldTests(IsolatedHome):
    def test_random_reference_codes_do_not_split_groups(self):
        self.assertEqual(merchant_key("INTERMON BBPZCBG"), merchant_key("INTERMON BBQDDLK"))
        self.assertEqual(classify("INTERMON BBPZCBG", -15, [])[0], "Donations")

    def test_local_shops(self):
        for text, cat in (("GUISSONA LES", "Groceries"), ("TASCA MOLINESA", "Restaurants & bars"),
                          ("COLCHONES AZNAR", "Home & furniture"), ("JACA SALUD", "Health"),
                          ("COMPLEJ PISCINA", "Leisure"), ("CC ARAGONIA", "Shopping")):
            self.assertEqual(classify(text, -10, [])[0], cat, text)

    def test_contactless_is_generic_and_asked_one_by_one(self):
        db = Database("pw")
        db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                               balance=0, updated_at="t"))
        for i, amt in enumerate((-5.0, -40.0, -12.0)):
            db.upsert_transaction(dict(id=f"c{i}", account_id="a", date="2026-01-0%d" % (i + 1),
                                       amount=amt, description="TRANSACCION CONTACTLESS",
                                       category="uncategorized"))
        categorize.categorize_all(db)
        self.assertEqual(categorize.review_queue(db), [])  # no shared rule suggested
        g = categorize.generic_entries(db)
        self.assertEqual(g["total"], 3)
        self.assertEqual([e["amount"] for e in g["entries"]], [-40.0, -12.0, -5.0])
        categorize.set_manual(db, "c1", "Groceries")
        self.assertEqual(categorize.generic_entries(db)["total"], 2)
        # a manual choice for one does not touch the others
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Groceries'").fetchone()[0], 1)


class CategoryNamesTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="t", name="A", kind="cash", currency="EUR",
                                    balance=0, updated_at="t"))

    def add(self, id, desc, amount=-10, category="uncategorized", source=""):
        self.db.upsert_transaction(dict(id=id, account_id="a", date="2026-01-01", amount=amount,
                                        description=desc, category=category, cat_source=source))

    def cat(self, id):
        return self.db.conn.execute("SELECT category FROM transactions WHERE id=?", (id,)).fetchone()[0]

    def test_proposed_merges_and_new_category_in_the_list(self):
        names = categorize.all_categories(self.db)
        for new in ("Home & furniture", "Utilities & telecom", "Leisure", "Taxes & fees", "Transport"):
            self.assertIn(new, names)
        for old in ("Fuel", "Telecom", "Utilities", "Entertainment", "Sports & leisure",
                    "Transfers sent", "Fees & interest", "Taxes"):
            self.assertNotIn(old, names)

    def test_old_names_are_moved_everywhere_including_your_own_choices(self):
        self.add("1", "REPSOL", category="Fuel", source="default")
        self.add("2", "WHATEVER", category="Telecom", source="manual")
        self.add("3", "SOMETHING", category="Sports & leisure", source="connector")
        self.db.conn.execute("INSERT INTO rules VALUES('xyz', 'Entertainment')")
        categorize.categorize_all(self.db)
        self.assertEqual((self.cat("1"), self.cat("2"), self.cat("3")),
                         ("Transport", "Utilities & telecom", "Leisure"))
        self.assertEqual(self.db.conn.execute("SELECT category FROM rules").fetchone()[0], "Leisure")
        self.assertEqual(categorize.categorize_all(self.db), 0)  # stable, nothing flickers

    def test_user_rule_with_an_old_name_lands_in_the_new_one(self):
        self.add("1", "PAGO XYZ SL")
        self.db.conn.execute("INSERT INTO rules VALUES('xyz', 'Fuel')")
        categorize.categorize_all(self.db)
        self.assertEqual(self.cat("1"), "Transport")

    def test_merge_and_rename_stick_for_future_entries(self):
        self.add("1", "MERCADONA")
        categorize.categorize_all(self.db)
        self.assertEqual(self.cat("1"), "Groceries")
        categorize.merge_category(self.db, "Groceries", "Food")  # rename
        self.assertEqual(self.cat("1"), "Food")
        self.add("2", "MERCADONA CENTRO")  # arrives in a later sync
        categorize.categorize_all(self.db)
        self.assertEqual(self.cat("2"), "Food")
        categorize.merge_category(self.db, "Food", "Shopping")  # chains are flattened
        categorize.add_rule(self.db, "mercadona", "Groceries")
        self.assertEqual((self.cat("1"), self.cat("2")), ("Shopping", "Shopping"))
        self.assertNotIn("Food", categorize.all_categories(self.db))

    def test_merge_validation(self):
        for src, dst in (("", "A"), ("A", ""), ("A", "A"), ("uncategorized", "A"), ("A", "uncategorized")):
            with self.assertRaises(ValueError):
                categorize.merge_category(self.db, src, dst)


class DbFlowTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        run_sync(self.db, {}, ["demo"])

    def test_demo_is_categorized_but_leaves_unclear_ones(self):
        queue = categorize.review_queue(self.db)
        keys = {g["key"] for g in queue}
        self.assertTrue(any("xyz" in k for k in keys))
        self.assertTrue(any("taller" in k for k in keys))
        self.assertNotIn("mercadona madrid", keys)

    def test_rule_resolves_queue_and_survives_resync(self):
        n = categorize.add_rule(self.db, "xyz servicios", "Shopping")
        self.assertGreater(n, 0)
        self.assertFalse(any("xyz" in g["key"] for g in categorize.review_queue(self.db)))
        run_sync(self.db, {}, ["demo"])
        self.assertFalse(any("xyz" in g["key"] for g in categorize.review_queue(self.db)))

    def test_manual_choice_is_never_overwritten_by_rules(self):
        tx = self.db.conn.execute(
            "SELECT id FROM transactions WHERE description LIKE '%MERCADONA%' LIMIT 1").fetchone()[0]
        categorize.set_manual(self.db, tx, "Kids")
        categorize.add_rule(self.db, "mercadona", "Other")
        cat = self.db.conn.execute("SELECT category FROM transactions WHERE id=?", (tx,)).fetchone()[0]
        self.assertEqual(cat, "Kids")

    def test_delete_rule_reverts(self):
        categorize.add_rule(self.db, "xyz servicios", "Shopping")
        categorize.delete_rule(self.db, "xyz servicios")
        self.assertTrue(any("xyz" in g["key"] for g in categorize.review_queue(self.db)))

    def test_internal_transfers_excluded_from_totals(self):
        flow = analysis.monthly_cashflow(self.db, 12)
        spent = sum(m["spending"] for m in flow)
        raw = self.db.conn.execute("SELECT SUM(-amount) FROM transactions WHERE amount<0").fetchone()[0]
        self.assertLess(spent, raw)
        cats = {c["category"] for m in flow for c in analysis.spending_by_category(self.db, m["month"])}
        self.assertNotIn(INTERNAL, cats)

    def test_spending_view(self):
        s = analysis.spending(self.db)
        self.assertEqual(s["month"], s["months"][0])
        self.assertAlmostEqual(sum(c["total"] for c in s["categories"]), s["total"], places=1)
        self.assertGreater(s["unclear"], 0)

    def test_migration_adds_column_to_old_database(self):
        db = Database("pw")
        db.conn.execute("ALTER TABLE transactions DROP COLUMN cat_source")
        db._migrate()
        cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(transactions)")}
        self.assertIn("cat_source", cols)


class ApiTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, {}))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def call(self, path, body=None, headers=None):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("X-Requested-With", "finances")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_review_categorize_roundtrip(self):
        code, queue = self.call("/api/review")
        self.assertEqual(code, 200)
        first = queue[0]
        code, res = self.call("/api/categorize", {"pattern": first["pattern"], "category": "Other"})
        self.assertEqual(code, 200)
        self.assertGreater(res["changed"], 0)
        self.assertNotIn(first["key"], {g["key"] for g in self.call("/api/review")[1]})
        self.assertIn(["Other"], [[r["category"]] for r in self.call("/api/rules")[1]])

    def test_merge_endpoint(self):
        code, res = self.call("/api/categories/merge", {"src": "Groceries", "dst": "Food"})
        self.assertEqual(code, 200)
        self.assertGreater(res["moved"], 0)
        self.assertIn("Food", {c["category"] for c in self.call("/api/spending")[1]["categories"]})
        self.assertEqual(self.call("/api/categories/merge", {"src": "x", "dst": "x"})[0], 400)

    def test_validation_and_csrf(self):
        self.assertEqual(self.call("/api/categorize", {"pattern": "x", "category": ""})[0], 400)
        self.assertEqual(self.call("/api/categorize", {"tx_id": "nope", "category": "A"})[0], 400)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/categorize",
                                     data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_transactions_filter(self):
        code, txs = self.call("/api/transactions?category=Salary&limit=3")
        self.assertEqual(code, 200)
        self.assertTrue(txs and all(t["category"] == "Salary" for t in txs))
        self.assertEqual(self.call("/api/transactions?limit=abc")[0], 400)


if __name__ == "__main__":
    unittest.main()
