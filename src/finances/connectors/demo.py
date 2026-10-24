"""Deterministic fake data so the dashboard works without any real account."""
import random
from datetime import date, datetime, timedelta, timezone

from .base import Connector, Snapshot

_SHOPS = [
    ("COMPRA TARJ. 5540XXXX MERCADONA MADRID", (-95, -12), 0.14),
    ("COMPRA TARJ. 5540XXXX CARREFOUR EXPRESS", (-45, -8), 0.06),
    ("PAGO MOVIL EN GLOVO", (-35, -12), 0.05),
    ("COMPRA TARJ. 5540XXXX RESTAURANTE EL RINCON", (-60, -20), 0.05),
    ("COMPRA TARJ. 5540XXXX REPSOL ESTACION", (-70, -35), 0.04),
    ("COMPRA TARJ. 5540XXXX AMAZON EU", (-120, -10), 0.05),
    ("COMPRA TARJ. 5540XXXX CAFETERIA LA ESQUINA", (-9, -2), 0.1),
    ("COMPRA TARJ. 5540XXXX PAGO XYZ SERVICIOS SL", (-40, -15), 0.02),  # unclear on purpose
    ("COMPRA TARJ. 5540XXXX TALLER PEPE 4471", (-180, -60), 0.008),    # unclear on purpose
]
_MONTHLY = [  # (day of month, description, amount or (min, max))
    (1, "ALQUILER VIVIENDA", -750.0),
    (3, "BASIC-FIT GIMNASIO", -39.90),
    (5, "NETFLIX.COM", -12.99),
    (12, "SPOTIFY AB", -10.99),
    (15, "RECIBO IBERDROLA CLIENTES", (-82.0, -48.0)),
    (20, "RECIBO MOVISTAR", -49.90),
    (28, "NOMINA ACME SL", 2400.0),
    (29, "TRANSFERENCIA A TRADE REPUBLIC", -300.0),
]


class DemoConnector(Connector):
    name = "demo"

    def fetch(self, user_input: str | None = None) -> Snapshot:
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
        for back in range(210):
            day = today - timedelta(days=back)
            for dom, desc, amount in _MONTHLY:
                if day.day == dom:
                    if isinstance(amount, tuple):
                        amount = round(rng.uniform(*amount), 2)
                    n += 1
                    snap.transactions.append(self._tx(n, "demo-current", day, amount, desc))
            if day.day == 17 and rng.random() < 0.5:
                n += 1
                snap.transactions.append(self._tx(n, "demo-current", day, 25.0, "BIZUM DE ANA GARCIA"))
            for desc, (hi, lo), prob in _SHOPS:
                if rng.random() < prob:
                    n += 1
                    snap.transactions.append(self._tx(n, "demo-card", day,
                                                      round(rng.uniform(hi, lo), 2), desc))
        return snap

    @staticmethod
    def _tx(n, account, day, amount, desc):
        # Always 'uncategorized': the real categorizer decides, as for a real bank.
        return dict(id=f"demo-{n}", account_id=account, date=day.isoformat(), amount=amount,
                    description=desc, category="uncategorized")
