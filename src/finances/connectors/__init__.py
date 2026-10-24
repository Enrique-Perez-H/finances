from .demo import DemoConnector
from .enable_banking import EnableBankingConnector
from .trade_republic import TradeRepublicConnector

REGISTRY = {c.name: c for c in (DemoConnector, EnableBankingConnector, TradeRepublicConnector)}
