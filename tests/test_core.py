import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from isolated import IsolatedHome  # noqa: E402
from finances import analysis, vault  # noqa: E402
from finances.db import Database  # noqa: E402
from finances.server import make_handler  # noqa: E402
from finances.sync import run_sync  # noqa: E402


class VaultTests(unittest.TestCase):
    def test_roundtrip_and_wrong_passphrase(self):
        blob = vault.encrypt("pw", b"secret data")
        self.assertNotIn(b"secret", blob)
        self.assertEqual(vault.decrypt("pw", blob), b"secret data")
        with self.assertRaises(vault.WrongPassphrase):
            vault.decrypt("nope", blob)

    def test_tamper_detected(self):
        blob = bytearray(vault.encrypt("pw", b"x" * 50))
        blob[-1] ^= 1
        with self.assertRaises(vault.WrongPassphrase):
            vault.decrypt("pw", bytes(blob))


class DbAndAnalysisTests(IsolatedHome):
    def test_persist_encrypted_and_reload(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        raw = (__import__("finances.config", fromlist=["x"]).db_path()).read_bytes()
        self.assertTrue(raw.startswith(vault.MAGIC))
        self.assertNotIn(b"Demo Current", raw)
        again = Database("pw")
        self.assertGreater(analysis.net_worth(again)["total"], 0)
        with self.assertRaises(vault.WrongPassphrase):
            Database("wrong")

    def test_sync_is_idempotent_and_reports_unimplemented(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        n = db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        status = run_sync(db, {}, ["demo", "trade_republic"])
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], n)
        self.assertEqual(status["trade_republic"]["state"], "error")  # no credentials configured
        self.assertIn("tr_phone", status["trade_republic"]["error"])
        self.assertEqual(run_sync(db, {}, ["enable_banking"])["enable_banking"]["state"], "unavailable")

    def test_delete_provider_removes_only_that_provider(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        db.upsert_account(dict(id="x", provider="real", name="Real", kind="cash", currency="EUR",
                               balance=5, updated_at="t"))
        self.assertEqual(db.delete_provider("demo"), 3)
        self.assertEqual([r["id"] for r in db.conn.execute("SELECT id FROM accounts")], ["x"])
        for table in ("transactions", "holdings"):
            self.assertEqual(db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_summary_shape(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        s = analysis.summary(db)
        self.assertEqual(len(s["projection"]), 10)
        self.assertTrue(s["cashflow"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.mkdtemp()
        os.environ["FINANCES_HOME"] = cls._home
        db = Database("pw")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, {"sources": ["demo"]}))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, method="GET", headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   headers=headers or {}, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_dashboard_and_api(self):
        self.assertEqual(self.req("/")[0], 200)
        code, body = self.req("/api/summary")
        self.assertEqual(code, 200)
        self.assertIn("net_worth", json.loads(body))

    def test_sync_requires_header_and_blocks_foreign_host(self):
        self.assertEqual(self.req("/api/sync", "POST")[0], 403)
        self.assertEqual(self.req("/api/sync", "POST", {"X-Requested-With": "finances"})[0], 200)
        self.assertEqual(self.req("/api/summary", headers={"Host": "evil.example"})[0], 403)

    def test_no_path_traversal(self):
        self.assertEqual(self.req("/../config.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
