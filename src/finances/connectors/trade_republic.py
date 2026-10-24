"""Trade Republic via the unofficial `pytr` library (read-only commands only).

TODO: implement. Needs phone + PIN in secrets (keys `tr_phone`, `tr_pin`) and a
2FA code, which is requested through NeedsInput. Unofficial: may break or
conflict with the provider's terms; use at your own risk.
"""
from .base import Connector, Snapshot


class TradeRepublicConnector(Connector):
    name = "trade_republic"

    def fetch(self) -> Snapshot:
        raise NotImplementedError("Trade Republic connector not implemented yet")
