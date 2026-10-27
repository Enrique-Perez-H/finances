import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from isolated import IsolatedHome
from finances import backup, config, vault
from finances.db import Database
from finances.server import make_handler
from finances.sync import run_sync

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


class BackupTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = Database("pw")
        run_sync(self.db, {}, ["demo"])
        config.save_secrets("pw", {"sources": ["demo"], "tr_pin": "1234"})
        self.out = tempfile.mkdtemp()

    def test_backup_holds_only_encrypted_files_and_a_manifest(self):
        path = backup.write_backup(self.out, NOW)
        self.assertEqual(path.name, "finances-backup-2026-10-27.zip")
        with zipfile.ZipFile(path) as z:
            self.assertEqual(sorted(z.namelist()), ["finances.db.enc", "manifest.json", "secrets.enc"])
            blob = b"".join(z.read(n) for n in z.namelist() if n != "manifest.json")
            self.assertTrue(z.read("finances.db.enc").startswith(vault.MAGIC))
        for secret in (b"Demo Current", b"1234", b"sources"):
            self.assertNotIn(secret, blob)  # nothing readable inside
        self.assertEqual(backup.last_backup(), NOW.isoformat(timespec="seconds"))

    def test_restore_roundtrip_on_a_fresh_phone(self):
        path = backup.write_backup(self.out, NOW)
        before = config.load_secrets("pw")
        for f in ("finances.db.enc", "secrets.enc"):
            os.remove(Path(self.home) / f)  # the phone is gone
        restored = backup.restore_backup(path, "pw")
        self.assertEqual(sorted(restored), ["finances.db.enc", "secrets.enc"])
        again = Database("pw")
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 3)
        self.assertEqual(config.load_secrets("pw"), before)

    def test_restore_refuses_wrong_passphrase_damage_and_overwrite(self):
        path = backup.write_backup(self.out, NOW)
        with self.assertRaises(vault.WrongPassphrase):
            backup.restore_backup(path, "nope", force=True)
        with self.assertRaises(ValueError):  # a vault exists
            backup.restore_backup(path, "pw")
        backup.restore_backup(path, "pw", force=True)  # allowed, the old one is kept aside
        self.assertTrue(list(Path(self.home).glob("finances.db.enc.before-restore-*")))
        damaged = Path(self.out) / "damaged.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(damaged, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                dst.writestr(n, data[:-1] + b"\x00" if n == "finances.db.enc" else data)
        with self.assertRaises(ValueError) as ctx:
            backup.restore_backup(damaged, "pw", force=True)
        self.assertIn("damaged", str(ctx.exception))
        junk = Path(self.out) / "junk.zip"
        with zipfile.ZipFile(junk, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(ValueError):
            backup.restore_backup(junk, "pw", force=True)

    def test_nothing_to_back_up(self):
        for f in ("finances.db.enc", "secrets.enc"):
            os.remove(Path(self.home) / f)
        with self.assertRaises(ValueError):
            backup.make_backup_bytes(NOW)

    def test_rolling_copies_one_per_day_keep_seven(self):
        folder = Path(self.home) / "backups"
        for day in range(1, 11):
            backup.rolling_copy(date(2026, 10, day))
        names = sorted(p.name for p in folder.glob("db-*.enc"))
        self.assertEqual(len(names), 7)
        self.assertEqual(names[-1], "db-2026-10-10.enc")
        backup.rolling_copy(date(2026, 10, 10))  # same day again: nothing new
        self.assertEqual(len(list(folder.glob("db-*.enc"))), 7)

    def test_save_keeps_a_daily_copy(self):
        self.db.save()
        self.assertTrue(list((Path(self.home) / "backups").glob("db-*.enc")))


class ApiTests(IsolatedHome):
    def test_download_through_the_server(self):
        db = Database("pw")
        run_sync(db, {}, ["demo"])
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, {}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/api/backup", data=b"{}", headers={"X-Requested-With": "finances"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.headers["Content-Type"], "application/zip")
                self.assertIn("attachment; filename=", r.headers["Content-Disposition"])
                self.assertIn("finances.db.enc", zipfile.ZipFile(io.BytesIO(r.read())).namelist())
            with urllib.request.urlopen(base + "/api/backup/status") as r:
                self.assertTrue(json.loads(r.read())["last"])
            with self.assertRaises(urllib.error.HTTPError) as ctx:  # needs the custom header
                urllib.request.urlopen(urllib.request.Request(base + "/api/backup", data=b"{}"))
            self.assertEqual(ctx.exception.code, 403)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
