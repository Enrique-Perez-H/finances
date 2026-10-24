"""Connector contract.

Connectors are READ-ONLY by construction: this interface has no method that can
move money, place orders or change anything at the provider. Keep it that way,
and request read-only scopes / tokens from every provider.
"""
from dataclasses import dataclass, field


class NeedsInput(Exception):
    """Raised when a sync needs the user (e.g. a 2FA code or bank re-consent).

    The connector instance is kept alive by the sync layer, so the next
    `fetch(user_input=...)` call can continue where this one stopped.
    """

    def __init__(self, prompt: str, url: str | None = None, field: str | None = None):
        super().__init__(prompt)
        self.prompt = prompt
        self.url = url
        self.field = field  # e.g. "code" -> the UI shows a text box


class ConnectorError(Exception):
    """An error whose message is safe to show in the UI (no secrets, no raw responses)."""


@dataclass
class Snapshot:
    accounts: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    holdings: dict[str, list[dict]] = field(default_factory=dict)  # account_id -> rows


class Connector:
    name = "base"

    def __init__(self, secrets: dict):
        # Mutable on purpose: connectors may store session tokens here. The sync
        # layer persists the dict (encrypted) after each run.
        self.secrets = secrets

    def fetch(self, user_input: str | None = None) -> Snapshot:
        raise NotImplementedError
