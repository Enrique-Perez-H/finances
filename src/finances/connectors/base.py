"""Connector contract.

Connectors are READ-ONLY by construction: this interface has no method that can
move money, place orders or change anything at the provider. Keep it that way,
and request read-only scopes / tokens from every provider.
"""
from dataclasses import dataclass, field


class NeedsInput(Exception):
    """Raised when a sync needs the user (e.g. a 2FA code or bank re-consent)."""

    def __init__(self, prompt: str, url: str | None = None):
        super().__init__(prompt)
        self.prompt = prompt
        self.url = url


@dataclass
class Snapshot:
    accounts: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    holdings: dict[str, list[dict]] = field(default_factory=dict)  # account_id -> rows


class Connector:
    name = "base"

    def __init__(self, secrets: dict):
        self.secrets = secrets

    def fetch(self) -> Snapshot:
        raise NotImplementedError
