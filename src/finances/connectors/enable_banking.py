"""Santander and Revolut via Enable Banking (PSD2 Account Information, read-only).

TODO: implement. Needs an application id and RSA private key in secrets
(`eb_app_id`, `eb_private_key`) to sign JWTs. Consent expires every few months;
raise NeedsInput with the bank authorization URL when re-approval is due.
"""
from .base import Connector, Snapshot


class EnableBankingConnector(Connector):
    name = "enable_banking"

    def fetch(self, user_input: str | None = None) -> Snapshot:
        raise NotImplementedError("Enable Banking connector not implemented yet")
