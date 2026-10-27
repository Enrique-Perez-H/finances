import json
import os
import stat
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from isolated import IsolatedHome
from finances import backup, cli, health
from finances.db import Database
from finances.server import make_handler, serve
from finances.sync import run_sync

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class HealthTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        self.db.upsert_account(dict(id="a", provider="santander", name="A", kind="cash", currency="EUR",
                                    balance=1, updated_at=(NOW - timedelta(hours=3)).isoformat()))
        self.db.upsert_account(dict(id="b", provider="trade_republic", name="B", kind="cash", currency="EUR",
                                    balance=1, updated_at=(NOW - timedelta(days=3)).isoformat()))

    def texts(self, **kw):
        return [w["text"] for w in health.status(self.db, kw.get("secrets", {}), NOW)["warnings"]]

    def test_last_sync_per_provider(self):
        st = health.status(self.db, {}, NOW)
        self.assertEqual([(s["provider"], s["hours_ago"]) for s in st["synced"]],
                         [("santander", 3.0), ("trade_republic", 72.0)])

    def test_no_backup_then_recent_then_old(self):
        self.assertTrue(any("never made a backup" in t for t in self.texts()))
        backup.mark_backup(NOW - timedelta(days=2))
        self.assertFalse(any("backup" in t for t in self.texts()))
        backup.mark_backup(NOW - timedelta(days=45))
        self.assertTrue(any("45 days old" in t for t in self.texts()))

    def test_consent_expiry_warnings(self):
        backup.mark_backup(NOW)
        soon = {"eb_session": {"valid_until": (NOW + timedelta(days=5)).isoformat()}}
        self.assertTrue(any("expires in 5 days" in t for t in self.texts(secrets=soon)))
        gone = {"eb_session": {"valid_until": (NOW - timedelta(days=1)).isoformat()}}
        warning = health.status(self.db, gone, NOW)["warnings"][0]
        self.assertEqual(warning["level"], "bad")
        fine = {"eb_session": {"valid_until": (NOW + timedelta(days=60)).isoformat()}}
        self.assertEqual(self.texts(secrets=fine), [])

    def test_unclear_entries_counted_not_paired_ones(self):
        backup.mark_backup(NOW)
        for i, (amount, day) in enumerate([(-30, "2026-10-01"), (30, "2026-10-03"), (-7, "2026-10-04")]):
            self.db.upsert_transaction(dict(id=str(i), account_id="a", date=day, amount=amount,
                                            description=f"x{i}", category="uncategorized"))
        from finances import categorize
        categorize.categorize_all(self.db)  # -30 / +30 cancel out
        st = health.status(self.db, {}, NOW)
        self.assertEqual(st["unclear"], 1)
        self.assertEqual(st["warnings"][0]["tab"], "spending")

    def test_stale_sync(self):
        backup.mark_backup(NOW)
        self.db.conn.execute("UPDATE accounts SET updated_at=?", ((NOW - timedelta(days=10)).isoformat(),))
        self.assertTrue(any("Nothing has synced" in t for t in self.texts()))

    def test_demo_placeholder_dates_do_not_crash(self):
        self.db.conn.execute("UPDATE accounts SET updated_at='t'")
        self.assertEqual(health.status(self.db, {}, NOW)["synced"], [])

    def test_endpoint(self):
        run_sync(self.db, {}, ["demo"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, {}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/health") as r:
                data = json.loads(r.read())
            self.assertEqual(set(data), {"synced", "unclear", "consent", "backup", "warnings"})
        finally:
            srv.shutdown()


class ShortcutAndOpenTests(IsolatedHome):
    def test_shortcut_script(self):
        home = self.home + "/phonehome"
        os.makedirs(home)
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            cli.main(["shortcut"])
        finally:
            os.environ["HOME"] = old
        path = Path(home) / ".shortcuts" / "Finances"
        text = path.read_text()
        self.assertTrue(text.startswith("#!/data/data/com.termux/files/usr/bin/bash"))
        self.assertIn("finances serve --open", text)
        self.assertTrue(path.stat().st_mode & stat.S_IXUSR)

    def test_serve_opens_the_browser_only_when_asked(self):
        opened = []

        class Stop(Exception):
            pass

        import finances.server as server
        db = Database("pw")
        original = server.ThreadingHTTPServer

        class Quick(original):
            def serve_forever(self, *a, **k):
                raise KeyboardInterrupt

        server.ThreadingHTTPServer = Quick
        try:
            serve(db, {}, 0, open_it=True, opener=opened.append)
            serve(db, {}, 0, open_it=False, opener=opened.append)
        finally:
            server.ThreadingHTTPServer = original
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("http://localhost:"))


if __name__ == "__main__":
    unittest.main()
