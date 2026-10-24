import json
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from finances.connectors.base import NeedsInput
from finances.connectors.enable_banking import (
    EnableBankingConnector, EnableBankingError, extract_code, make_jwt, map_transaction)

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()


class FakeBank:
    def __init__(self):
        self.calls = []
        self.dead = False

    required = []

    def __call__(self, method, path, body=None, params=None, headers=None):
        self.calls.append((method, path, body, params))
        self.header_log = getattr(self, "header_log", []) + [(path, headers)]
        if path == "/aspsps":
            return {"aspsps": [{"name": "Santander", "country": "ES",
                                "maximum_consent_validity": 90 * 86400,
                                "required_psu_headers": self.required},
                               {"name": "Santander Consumer", "country": "ES"}]}
        if path == "/auth":
            return {"url": "https://bank.example/approve"}
        if path == "/sessions":
            return {"session_id": "s1", "aspsp": {"name": "Santander"},
                    "access": {"valid_until": (NOW + timedelta(days=89)).isoformat()},
                    "accounts": [
                        {"uid": "u1", "identification_hash": "abcdef1234567890", "currency": "EUR",
                         "cash_account_type": "CACC", "product": "Cuenta",
                         "account_id": {"iban": "ES9121000418450200051332"}},
                        {"uid": "u2", "identification_hash": "zzzzzzzzzzzzzzzz", "currency": "EUR",
                         "cash_account_type": "LOAN"}]}
        if self.dead:
            raise EnableBankingError(403)
        if path.endswith("/balances"):
            return {"balances": [
                {"balance_type": "ITAV", "balance_amount": {"amount": "999", "currency": "EUR"}},
                {"balance_type": "CLBD", "balance_amount": {"amount": "1500.25", "currency": "EUR"}}]}
        if path.endswith("/transactions"):
            if params.get("continuation_key") == "k2":
                return {"transactions": [self.tx("-5.00", "DBIT", "2026-01-02", "Cafe")]}
            return {"transactions": [
                self.tx("20.00", "DBIT", "2026-01-03", "Mercadona"),
                self.tx("20.00", "DBIT", "2026-01-03", "Mercadona"),
                self.tx("2000", "CRDT", "2026-01-01", "Payroll", name="Employer", ref="T9"),
                {**self.tx("1", "DBIT", "2026-01-04", "pending"), "status": "PDNG"}],
                "continuation_key": "k2"}
        raise AssertionError(path)

    @staticmethod
    def tx(amount, ind, date, remit, name=None, ref=None):
        d = {"transaction_amount": {"amount": amount, "currency": "EUR"},
             "credit_debit_indicator": ind, "status": "BOOK", "booking_date": date,
             "remittance_information": [remit]}
        if name:
            d["debtor" if ind == "CRDT" else "creditor"] = {"name": name}
        if ref:
            d["transaction_id"] = ref
        return d


def make(secrets=None, bank=None):
    s = {"eb_app_id": "app", "eb_private_key": PEM, **(secrets or {})}
    return EnableBankingConnector(s, http=bank or FakeBank(), now=lambda: NOW), s


class HelpersTests(unittest.TestCase):
    def test_jwt_is_rs256_and_verifiable(self):
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        token = make_jwt("app-id", PEM, NOW)
        h, p, sig = token.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(h))),
                         {"typ": "JWT", "alg": "RS256", "kid": "app-id"})
        claims = json.loads(base64.urlsafe_b64decode(pad(p)))
        self.assertEqual(claims["aud"], "api.enablebanking.com")
        self.assertLessEqual(claims["exp"] - claims["iat"], 86400)
        KEY.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{h}.{p}".encode(),
                                padding.PKCS1v15(), hashes.SHA256())

    def test_extract_code(self):
        self.assertEqual(extract_code(" abc "), "abc")
        self.assertEqual(extract_code("http://localhost:8765/callback?code=XYZ&state=s1", "s1"), "XYZ")
        with self.assertRaises(ValueError):
            extract_code("http://x/cb?code=XYZ&state=other", "s1")
        with self.assertRaises(ValueError):
            extract_code("http://x/cb?error=access_denied")

    def test_map_transaction_signs_and_duplicates(self):
        seen = Counter()
        a = map_transaction(FakeBank.tx("20.00", "DBIT", "2026-01-03", "Shop"), "acc", seen)
        b = map_transaction(FakeBank.tx("20.00", "DBIT", "2026-01-03", "Shop"), "acc", seen)
        self.assertEqual(a["amount"], -20.0)
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNone(map_transaction({**FakeBank.tx("1", "DBIT", "2026-01-01", "x"),
                                           "status": "PDNG"}, "acc", seen))


class ConnectorTests(unittest.TestCase):
    def test_missing_secrets(self):
        with self.assertRaises(RuntimeError):
            EnableBankingConnector({}).fetch()

    def test_first_run_asks_for_approval_with_bank_and_validity(self):
        c, _ = make()
        with self.assertRaises(NeedsInput) as ctx:
            c.fetch()
        self.assertEqual(ctx.exception.url, "https://bank.example/approve")
        self.assertEqual(ctx.exception.field, "code")
        auth = [x for x in c._http.calls if x[1] == "/auth"][0][2]
        self.assertEqual(auth["aspsp"], {"name": "Santander", "country": "ES"})
        self.assertEqual(auth["psu_type"], "personal")
        valid = datetime.fromisoformat(auth["access"]["valid_until"])
        self.assertLess(valid - NOW, timedelta(days=90))  # bank's own limit respected

    def test_ambiguous_bank_name_is_reported(self):
        c, _ = make({"eb_bank": "santand"})
        c._http = lambda *a, **k: {"aspsps": [{"name": "Santander", "country": "ES"},
                                              {"name": "Santander Consumer", "country": "ES"}]}
        with self.assertRaises(RuntimeError):
            c.fetch()

    def test_full_flow_data_and_session_persisted(self):
        c, secrets = make()
        with self.assertRaises(NeedsInput):
            c.fetch()
        snap = c.fetch(user_input=f"http://localhost:8765/callback?code=C1&state={c._state}")
        self.assertEqual(secrets["eb_session"]["session_id"], "s1")
        self.assertEqual(len(secrets["eb_session"]["accounts"]), 1)  # loan skipped
        self.assertNotIn("ES9121000418450200051332", json.dumps(secrets["eb_session"]))  # IBAN tail only
        acct = snap.accounts[0]
        self.assertEqual(acct["balance"], 1500.25)  # booked balance preferred over available
        self.assertIn("****1332", acct["name"])
        self.assertEqual(acct["provider"], "santander")
        amounts = sorted(t["amount"] for t in snap.transactions)
        self.assertEqual(amounts, [-20.0, -20.0, -5.0, 2000.0])  # pending dropped, pages merged
        self.assertEqual(len({t["id"] for t in snap.transactions}), 4)
        self.assertTrue(secrets["eb_session"]["initial_done"])
        # second sync: no approval needed, shorter history window
        calls_before = len(c._http.calls)
        secrets["eb_last_fetch"] = (NOW - timedelta(days=2)).isoformat()  # past the cool-down
        c.fetch()
        tx_call = [x for x in c._http.calls[calls_before:] if x[1].endswith("/transactions")][0]
        self.assertEqual(tx_call[3]["date_from"], (NOW - timedelta(days=60)).date().isoformat())

    def test_api_error_message_reaches_the_ui(self):
        from finances import sync
        def failing(method, path, body=None, params=None, headers=None):
            raise EnableBankingError(401, "Invalid JWT")
        c, secrets = make()
        c._http = failing
        sync._pending.clear()
        from finances.connectors import REGISTRY
        old = REGISTRY["enable_banking"]
        REGISTRY["enable_banking"] = lambda s: c
        try:
            class NoDb:
                pass
            st = sync.run_sync(NoDb(), secrets, ["enable_banking"])
        finally:
            REGISTRY["enable_banking"] = old
        self.assertEqual(st["enable_banking"]["state"], "error")
        self.assertIn("401", st["enable_banking"]["error"])
        self.assertIn("Invalid JWT", st["enable_banking"]["error"])

    def test_mcc_is_kept(self):
        tx = {**FakeBank.tx("5", "DBIT", "2026-01-01", "x"), "merchant_category_code": "5411"}
        self.assertEqual(map_transaction(tx, "acc", Counter())["mcc"], "5411")
        self.assertEqual(map_transaction(FakeBank.tx("5", "DBIT", "2026-01-01", "x"), "acc", Counter())["mcc"], "")

    def test_field_report_has_names_and_counts_but_no_values(self):
        c, secrets = make()
        with self.assertRaises(NeedsInput):
            c.fetch()
        c.fetch(user_input="C1")
        report = c.field_report()
        self.assertEqual(report["transactions"], 5)  # raw, pending included
        self.assertEqual(report["fields"]["booking_date"], 5)
        self.assertEqual(report["fields"]["debtor.name"], 1)
        self.assertNotIn("Mercadona", json.dumps(report))
        self.assertNotIn("20.00", json.dumps(report))

    PSU = {"Psu-Ip-Address": "192.0.2.7", "Psu-User-Agent": "Browser/1"}

    def connected(self, required, psu=True, bank=None):
        bank = bank or FakeBank()
        bank.required = required
        c, secrets = make(bank=bank)
        with self.assertRaises(NeedsInput):
            c.fetch()
        c.fetch(user_input="C1")
        c.set_psu(self.PSU if psu else None)
        return c, secrets, bank

    def data_headers(self, bank):
        return [h for path, h in bank.header_log if path.startswith("/accounts/")]

    def test_psu_headers_sent_when_required_and_present(self):
        c, secrets, bank = self.connected(["Psu-Ip-Address", "Psu-User-Agent"])
        bank.header_log = []
        secrets["eb_last_fetch"] = (NOW - timedelta(hours=1)).isoformat()
        c.fetch()
        self.assertTrue(self.data_headers(bank))
        for h in self.data_headers(bank):
            self.assertEqual(h, self.PSU)

    def test_all_or_none_and_never_invented(self):
        c, secrets, bank = self.connected(["Psu-Ip-Address", "Psu-Geo-Location"])  # geo unavailable
        bank.header_log = []
        secrets["eb_last_fetch"] = (NOW - timedelta(days=2)).isoformat()
        c.fetch()
        self.assertTrue(all(h == {} for h in self.data_headers(bank)))

    def test_no_headers_without_a_present_user(self):
        c, secrets, bank = self.connected(["Psu-Ip-Address"], psu=False)
        bank.header_log = []
        secrets["eb_last_fetch"] = (NOW - timedelta(days=2)).isoformat()
        c.fetch()
        self.assertTrue(all(h == {} for h in self.data_headers(bank)))

    def test_unattended_syncs_are_spaced_out(self):
        from finances.connectors.base import Skipped
        c, secrets, bank = self.connected([], psu=False)
        with self.assertRaises(Skipped) as ctx:
            c.fetch()  # just fetched when connecting
        self.assertIn("min", str(ctx.exception))
        secrets["eb_last_fetch"] = (NOW - timedelta(hours=25)).isoformat()
        c.fetch()  # one account => 2 calls per sync => allowed again well within a day

    def test_attended_syncs_only_wait_minutes(self):
        from finances.connectors.base import Skipped
        c, secrets, bank = self.connected(["Psu-Ip-Address"])
        secrets["eb_last_fetch"] = (NOW - timedelta(minutes=3)).isoformat()
        with self.assertRaises(Skipped):
            c.fetch()
        secrets["eb_last_fetch"] = (NOW - timedelta(minutes=11)).isoformat()
        c.fetch()

    def test_rate_limit_gets_a_clear_message_and_cooldown(self):
        from finances.connectors.base import ConnectorError
        c, secrets, bank = self.connected(["Psu-Ip-Address"])
        secrets["eb_last_fetch"] = (NOW - timedelta(days=2)).isoformat()
        def limited(method, path, body=None, params=None, headers=None):
            raise EnableBankingError(429, "[HUB046] Allowed number of accesses exceeded for consent.")
        c._http = limited
        with self.assertRaises(ConnectorError) as ctx:
            c.fetch()
        self.assertIn("24 hours", str(ctx.exception))
        self.assertIn("eb_session", secrets)  # a rate limit does not discard the consent
        self.assertEqual(secrets["eb_last_fetch"], NOW.isoformat())

    def test_expired_session_asks_again(self):
        c, secrets = make()
        secrets["eb_session"] = {"session_id": "old", "accounts": [], "initial_done": True,
                                 "valid_until": (NOW - timedelta(days=1)).isoformat()}
        with self.assertRaises(NeedsInput):
            c.fetch()

    def test_revoked_session_restarts_authorization(self):
        bank = FakeBank()
        c, secrets = make(bank=bank)
        with self.assertRaises(NeedsInput):
            c.fetch()
        c.fetch(user_input="C1")
        bank.dead = True
        secrets["eb_last_fetch"] = (NOW - timedelta(days=2)).isoformat()
        with self.assertRaises(NeedsInput):
            c.fetch()
        self.assertNotIn("eb_session", secrets)


if __name__ == "__main__":
    unittest.main()
