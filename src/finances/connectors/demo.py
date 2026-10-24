"""Deterministic fake data so the dashboard works without any real account."""
import random
from datetime import date, datetime, timedelta, timezone

from .base import Connector, Snapshot

_CATEGORIES = {
    "Groceries": (-95, -12),
    "Restaurants": (-60, -9),
    "Transport": (-40, -3),
    "Subscriptions": (-18, -5),
    "Shopping": (-120, -15),
}


class DemoConnector(Connector):
    name = "demo"

    def fetch(self) -> Snapshot:
        rng = random.Random(42)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snap = Snapshot()
        snap.accounts = [
            dict(id="demo-current", provider="demo", name="Demo Current", kind="cash",
                 currency="EUR", balance=3150.40, updated_at=now),
            dict(id="demo-card", provider="demo", name="Demo Card", kind="cash",
                 currency="EUR", balance=820.15, updated_at=now),
            dict(id="demo-broker", provider="demo", name="Demo Broker", kind="investment",
                 currency="EUR", balance=12480.00, updated_at=now),
        ]
        snap.holdings["demo-broker"] = [
            dict(account_id="demo-broker", symbol="WORLD-ETF", quantity=82.5,
                 value=9900.0, cost_basis=8700.0),
            dict(account_id="demo-broker", symbol="EM-ETF", quantity=60.0,
                 value=2580.0, cost_basis=2400.0),
        ]
        today = date.today()
        n = 0
        for back in range(180):
            day = today - timedelta(days=back)
            if day.day == 28:
                snap.transactions.append(self._tx(n := n + 1, "demo-current", day, 2400.0,
                                                  "Salary", "Income"))
            if day.day == 1:
                snap.transactions.append(self._tx(n := n + 1, "demo-current", day, -750.0,
                                                  "Rent", "Housing"))
            for cat, (hi, lo) in _CATEGORIES.items():
                if rng.random() < 0.12:
                    amt = round(rng.uniform(hi, lo), 2)
                    snap.transactions.append(self._tx(n := n + 1, "demo-card", day, amt,
                                                      f"{cat} purchase", cat))
        return snap

    @staticmethod
    def _tx(n, account, day, amount, desc, cat):
        return dict(id=f"demo-{n}", account_id=account, date=day.isoformat(),
                    amount=amount, description=desc, category=cat)
