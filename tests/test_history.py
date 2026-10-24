import asyncio
import unittest
from datetime import date

from isolated import IsolatedHome
from finances import categorize, history
from finances.connectors.trade_republic import fetch_timeline, map_timeline_item
from finances.db import Database
from finances.sync import run_sync

TODAY = date(2026, 6, 20)


def acct(db, id, kind, balance, name=None):
    db.upsert_account(dict(id=id, provider="t", name=name or id, kind=kind, currency="EUR",
                           balance=balance, updated_at="t"))


def tx(db, id, account, day, amount, desc="x", category="uncategorized", **kw):
    db.upsert_transaction(dict(id=id, account_id=account, date=day, amount=amount,
                               description=desc, category=category, **kw))


class EvolutionTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")

    def test_cash_is_rebuilt_backwards_from_today(self):
        acct(self.db, "c", "cash", 1000)
        tx(self.db, "1", "c", "2026-04-10", 300)
        tx(self.db, "2", "c", "2026-05-15", -100)
        tx(self.db, "3", "c", "2026-06-05", 200)
        rows = {r["month"]: r for r in history.evolution(self.db, today=TODAY)["rows"]}
        self.assertEqual(rows["2026-06"]["cash"], 1000.0)  # today
        self.assertEqual(rows["2026-05"]["cash"], 800.0)   # before the June +200
        self.assertEqual(rows["2026-04"]["cash"], 900.0)   # before the May -100
        self.assertNotIn("2026-03", rows)                  # before any transaction: unknown

    def test_account_with_transactions_is_rebuilt_even_if_typed_as_investment(self):
        acct(self.db, "tr-cash", "investment", 500)
        tx(self.db, "1", "tr-cash", "2026-04-10", 300)
        tx(self.db, "2", "tr-cash", "2026-05-15", 200)
        rows = {r["month"]: r for r in history.evolution(self.db, today=TODAY)["rows"]}
        self.assertEqual(rows["2026-04"]["investment"], 300.0)
        self.assertEqual(rows["2026-05"]["investment"], 500.0)

    def test_investments_known_only_from_first_snapshot(self):
        acct(self.db, "c", "cash", 100)
        tx(self.db, "1", "c", "2026-03-10", 100)
        acct(self.db, "i", "investment", 5000)
        history.snapshot(self.db, date(2026, 5, 20))
        acct(self.db, "i", "investment", 5200)
        rows = {r["month"]: r for r in history.evolution(self.db, today=TODAY)["rows"]}
        self.assertIsNone(rows["2026-03"]["investment"])
        self.assertTrue(rows["2026-03"]["partial"])
        self.assertEqual(rows["2026-05"]["investment"], 5000.0)
        self.assertEqual(rows["2026-06"]["investment"], 5200.0)  # carried until today's balance
        self.assertEqual(rows["2026-06"]["total"], 5300.0)

    def test_total_change_ignores_incomplete_months(self):
        acct(self.db, "c", "cash", 100)
        tx(self.db, "1", "c", "2026-04-10", 100)
        acct(self.db, "i", "investment", 5000)
        d = history.evolution(self.db, today=TODAY)
        self.assertTrue(d["rows"][0]["partial"])
        self.assertIsNone(d["changes"]["total"]["month"])  # +5000 would be a fake jump
        self.assertIsNone(d["changes"]["investment"]["month"])

    def test_changes_and_kind_override_survive_resync(self):
        acct(self.db, "tr-cash", "cash", 50)
        history.set_kind(self.db, "tr-cash", "investment")
        acct(self.db, "tr-cash", "cash", 60)  # a later sync reports it as cash again
        self.assertEqual(self.db.conn.execute("SELECT kind FROM accounts").fetchone()[0], "investment")
        with self.assertRaises(ValueError):
            history.set_kind(self.db, "tr-cash", "crypto")
        with self.assertRaises(ValueError):
            history.set_kind(self.db, "nope", "cash")

    def test_sync_records_a_snapshot(self):
        run_sync(self.db, {}, ["demo"])
        n = self.db.conn.execute("SELECT COUNT(*) FROM balance_history").fetchone()[0]
        self.assertEqual(n, 3)
        run_sync(self.db, {}, ["demo"])  # same day: replaced, not duplicated
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM balance_history").fetchone()[0], 3)

    def test_demo_evolution_has_all_kinds(self):
        run_sync(self.db, {}, ["demo"])
        d = history.evolution(self.db)
        self.assertGreater(len(d["rows"]), 3)
        self.assertIsNotNone(d["latest"]["investment"])

    def test_interest_summary(self):
        acct(self.db, "tr", "investment", 0, "Trade Republic Cash")
        for i, month in enumerate(("2026-04", "2026-05", "2026-06")):
            tx(self.db, f"i{i}", "tr", f"{month}-01", 10 + i, "Interest", "Interest & dividends",
               cat_source="connector")
        tx(self.db, "old", "tr", "2024-01-01", 99, "Interest", "Interest & dividends", cat_source="connector")
        i = history.interest(self.db, TODAY)
        self.assertEqual(i["last_12_months"], 33.0)
        self.assertEqual(i["total"], 132.0)
        self.assertEqual(i["average_per_month"], 2.75)
        self.assertEqual(i["by_account"][0]["account"], "Trade Republic Cash")

    def test_empty(self):
        self.assertEqual(history.evolution(self.db, today=TODAY)["rows"], [])


class ConnectorCategoriesTests(IsolatedHome):
    def test_connector_set_category_is_not_overwritten_by_rules(self):
        db = Database("pw")
        acct(db, "a", "cash", 0)
        tx(db, "1", "a", "2026-01-01", 5, "Zinsen", "Interest & dividends", cat_source="connector")
        categorize.add_rule(db, "zinsen", "Other")
        self.assertEqual(db.conn.execute("SELECT category FROM transactions").fetchone()[0],
                         "Interest & dividends")
        categorize.set_manual(db, "1", "Refunds")  # you can still override it by hand
        self.assertEqual(db.conn.execute("SELECT category FROM transactions").fetchone()[0], "Refunds")


def item(**kw):
    base = {"id": "e1", "timestamp": "2026-05-02T08:00:00.000+0000", "title": "Zinsen",
            "subtitle": "", "eventType": "INTEREST_PAYOUT", "amount": {"value": 12.34, "currency": "EUR"}}
    return {**base, **kw}


class TimelineMappingTests(unittest.TestCase):
    def test_interest(self):
        t = map_timeline_item(item())
        self.assertEqual((t["id"], t["date"], t["amount"], t["category"], t["cat_source"]),
                         ("tr-e1", "2026-05-02", 12.34, "Interest & dividends", "connector"))
        self.assertEqual(t["account_id"], "tr-cash")

    def test_moves_between_cash_and_investments_are_internal(self):
        for etype, amount in (("ORDER_EXECUTED", -100), ("SAVINGS_PLAN_EXECUTED", -50),
                              ("BANK_TRANSACTION_INCOMING", 500), ("PAYMENT_OUTBOUND", -20)):
            t = map_timeline_item(item(eventType=etype, amount={"value": amount}))
            self.assertEqual(t["category"], "Internal transfer", etype)

    def test_card_payment_is_left_for_the_rules(self):
        t = map_timeline_item(item(eventType="CARD_SUCCESSFUL_TRANSACTION", title="Mercadona",
                                   amount={"value": -23.5}))
        self.assertEqual((t["category"], t.get("cat_source")), ("uncategorized", None))
        self.assertEqual(t["description"], "Mercadona")

    def test_skipped_items(self):
        self.assertIsNone(map_timeline_item(item(amount={"value": 0})))
        self.assertIsNone(map_timeline_item(item(amount=None)))
        self.assertIsNone(map_timeline_item(item(status="CANCELED")))
        self.assertIsNone(map_timeline_item(item(id=None)))

    def test_negative_interest_is_not_labelled_as_income(self):
        t = map_timeline_item(item(amount={"value": -1}))
        self.assertEqual(t["category"], "uncategorized")


class FakeTimelineApi:
    def __init__(self, pages):
        self.pages, self.asked, self.unsubscribed = pages, [], []

    async def timeline_transactions(self, after=None):
        self.asked.append(after)
        return f"sub{len(self.asked)}"

    async def recv(self):
        return "sub", {}, self.pages[len(self.asked) - 1]

    async def unsubscribe(self, sub):
        self.unsubscribed.append(sub)


class FetchTimelineTests(unittest.TestCase):
    def run_fetch(self, pages, since="1970-01-01", max_pages=10):
        api = FakeTimelineApi(pages)
        return api, asyncio.run(fetch_timeline(api, since, max_pages))

    def test_pages_until_cursor_runs_out(self):
        pages = [{"items": [item(id="a")], "cursors": {"after": "c1"}},
                 {"items": [item(id="b")], "cursors": {}}]
        api, out = self.run_fetch(pages)
        self.assertEqual([t["id"] for t in out], ["tr-a", "tr-b"])
        self.assertEqual(api.asked, [None, "c1"])
        self.assertEqual(len(api.unsubscribed), 2)

    def test_stops_when_entries_get_older_than_the_window(self):
        pages = [{"items": [item(id="new", timestamp="2026-06-01T00:00:00.000+0000"),
                            item(id="old", timestamp="2025-01-01T00:00:00.000+0000")],
                  "cursors": {"after": "c1"}},
                 {"items": [item(id="never")], "cursors": {}}]
        api, out = self.run_fetch(pages, since="2026-05-01")
        self.assertEqual([t["id"] for t in out], ["tr-new"])
        self.assertEqual(len(api.asked), 1)

    def test_page_cap(self):
        pages = [{"items": [item(id=str(i))], "cursors": {"after": "next"}} for i in range(5)]
        api, out = self.run_fetch(pages, max_pages=2)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
