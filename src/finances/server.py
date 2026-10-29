"""Local-only JSON API + static dashboard, built on the standard library.

Bound to 127.0.0.1. To reach it from another device use a private network
(e.g. Tailscale); never expose it publicly. Mutating requests need a custom
header and the Host header is checked, to block cross-site and DNS-rebinding abuse.
"""
import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analysis, backup, categorize, config, health, forecast, fund, history, income, pairs, recurring, sonfund, yearly
from .db import Database
from .sync import run_sync

WEB = Path(__file__).parent / "web"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".svg": "image/svg+xml"}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def local_ip() -> str:
    """This device's own network address (no packet is sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class BadRequest(Exception):
    pass


def _year(value: str, db: Database, allow_all: bool = False) -> str:
    if allow_all and value == "all":
        return "all"
    if not (len(value) == 4 and value.isdigit()):
        years = yearly.years(db)
        if value == "" and years:
            return years[0]  # latest year with data
        raise ValueError("year must look like 2025")
    return value


def _q(query: dict, key: str, default=None):
    return query.get(key, [default])[0]


def _num(query: dict, key: str, default: float) -> float:
    try:
        return float(_q(query, key, default))
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number") from None


# GET routes: (db, query) -> JSON-able. Read-only.
GET_ROUTES = {
    "/api/summary": lambda db, q: analysis.summary(db),
    "/api/top": lambda db, q: analysis.top_entries(db, _q(q, "month", ""), int(_num(q, "n", 3))),
    "/api/cashflow": lambda db, q: analysis.monthly_cashflow(db, int(_num(q, "months", 600))),
    "/api/spending/average": lambda db, q: analysis.average_month(
        db, int(_num(q, "months", 12)) or None),
    "/api/spending": lambda db, q: analysis.spending(db, _q(q, "month")),
    "/api/entries": lambda db, q: analysis.entries(
        db, _q(q, "category"), _q(q, "since"), _q(q, "until"), _q(q, "q"), _q(q, "sort", "date"),
        int(_num(q, "offset", 0)), int(_num(q, "limit", 50))),
    "/api/transactions": lambda db, q: analysis.transactions(
        db, _q(q, "category"), _q(q, "month"), int(_num(q, "limit", 200))),
    "/api/income": lambda db, q: income.summary(db, int(_num(q, "months", 12))),
    "/api/recurring": lambda db, q: recurring.summary(db),
    "/api/forecast": lambda db, q: forecast.project(
        db, months=int(_num(q, "months", 24)), income_growth=_num(q, "income_growth", 0),
        inflation=_num(q, "inflation", 2), annual_return=_num(q, "return", 4),
        invest_share=_num(q, "invest_share", 0), extra_income=_num(q, "extra_income", 0),
        extra_cost=_num(q, "extra_cost", 0),
        include_other_income=_q(q, "other_income", "1") != "0"),
    "/api/funds": lambda db, q: fund.list_plans(db),
    "/api/sonfund": lambda db, q: sonfund.payload(db),
    "/api/evolution": lambda db, q: history.evolution(db, int(_num(q, "months", 24))),
    "/api/pairs": lambda db, q: pairs.listing(db),
    "/api/backup/status": lambda db, q: {"last": backup.last_backup()},
    "/api/yearly": lambda db, q: yearly.summary(db, _year(_q(q, "year", ""), db)),
    "/api/review": lambda db, q: categorize.review_queue(db),
    "/api/review/generic": lambda db, q: categorize.generic_entries(db),
    "/api/categories": lambda db, q: categorize.all_categories(db),
    "/api/rules": lambda db, q: [dict(r, ignored=r["pattern"] in categorize.GENERIC_PATTERNS)
                                 for r in db.conn.execute(
        "SELECT pattern, direction, category FROM rules ORDER BY pattern, direction")],
}


def _post_categorize(db, body):
    category = str(body.get("category", "")).strip()
    if "tx_id" in body:  # one entry only; never overwritten by rules afterwards
        categorize.set_manual(db, str(body["tx_id"]), category)
        changed = 1
    else:
        changed = categorize.add_rule(db, str(body.get("pattern", "")), category,
                                      str(body.get("direction", "any")))
    db.save()
    return {"changed": changed}


def _post_categorize_bulk(db, body):
    ids = body.get("tx_ids")
    if not isinstance(ids, list):
        raise ValueError("tx_ids must be a list")
    moved = categorize.set_manual_many(db, [str(i) for i in ids], str(body.get("category", "")))
    db.save()
    return {"moved": moved}


def _post_recurring_ignore(db, body):
    recurring.ignore(db, str(body.get("key", "")), str(body.get("direction", "")))
    db.save()
    return {"ok": True}


def _post_recurring_restore(db, body):
    recurring.restore(db, str(body.get("key", "")), str(body.get("direction", "")))
    db.save()
    return {"ok": True}


def _post_merge_category(db, body):
    moved = categorize.merge_category(db, str(body.get("src", "")), str(body.get("dst", "")))
    db.save()
    return {"moved": moved}


def _post_block_pair(db, body):
    pairs.block(db, str(body.get("tx_id", "")))
    db.save()
    return {"ok": True}


def _post_delete_rule(db, body):
    changed = categorize.delete_rule(db, str(body.get("pattern", "")), body.get("direction") or None)
    db.save()
    return {"changed": changed}


def _post_fund_save(db, body):
    plan = fund.save_plan(db, body)
    db.save()
    return plan


def _post_fund_delete(db, body):
    fund.delete_plan(db, body.get("id"))
    db.save()
    return {"ok": True}


def _post_account_kind(db, body):
    history.set_kind(db, str(body.get("id", "")), str(body.get("kind", "")))
    db.save()
    return {"ok": True}


def _post_sonfund(db, body):
    cfg = sonfund.save_config(db, body)
    db.save()
    return cfg


def _post_sonfund_deposit(db, body):
    dep = sonfund.add_deposit(db, body)
    db.save()
    return dep


def _post_sonfund_deposit_delete(db, body):
    sonfund.delete_deposit(db, body.get("id"))
    db.save()
    return {"ok": True}


# POST routes: (db, body) -> JSON-able. These change local data only.
POST_ROUTES = {
    "/api/accounts/kind": _post_account_kind,
    "/api/sonfund": _post_sonfund,
    "/api/sonfund/deposit": _post_sonfund_deposit,
    "/api/sonfund/deposit/delete": _post_sonfund_deposit_delete,
    "/api/funds": _post_fund_save,
    "/api/funds/delete": _post_fund_delete,
    "/api/funds/preview": lambda db, body: fund.preview(db, body),
    "/api/categorize": _post_categorize,
    "/api/categorize/bulk": _post_categorize_bulk,
    "/api/rules/delete": _post_delete_rule,
    "/api/pairs/block": _post_block_pair,
    "/api/recurring/ignore": _post_recurring_ignore,
    "/api/recurring/restore": _post_recurring_restore,
    "/api/categories/merge": _post_merge_category,
}


def make_handler(db: Database, secrets: dict, allowed_hosts=ALLOWED_HOSTS):
    lock = threading.Lock()       # database reads/writes
    sync_lock = threading.Lock()  # one sync at a time

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep paths/amounts out of logs
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in allowed_hosts

        def _send(self, code, body: bytes, ctype="application/json", filename=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise BadRequest("body too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise BadRequest("invalid JSON") from None
            if not isinstance(body, dict):
                raise BadRequest("JSON object expected")
            return body

        def _psu(self) -> dict:
            """Who is present: the person who tapped Sync, described from their own request."""
            ip = self.client_address[0]
            if ip in ("127.0.0.1", "::1"):
                ip = secrets.get("eb_psu_ip") or local_ip()
            h = self.headers
            pairs = {"Psu-Ip-Address": ip, "Psu-User-Agent": h.get("User-Agent"),
                     "Psu-Accept": h.get("Accept"), "Psu-Accept-Language": h.get("Accept-Language"),
                     "Psu-Accept-Encoding": h.get("Accept-Encoding"), "Psu-Referer": h.get("Referer")}
            return {k: v for k, v in pairs.items() if v}

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/health":
                with lock:
                    return self._json(200, health.status(db, secrets))
            route = GET_ROUTES.get(url.path)
            if route:
                try:
                    with lock:
                        return self._json(200, route(db, urllib.parse.parse_qs(url.query)))
                except (BadRequest, ValueError) as e:
                    return self._json(400, {"error": str(e)})
            # /callback is where the bank sends you back; the page reads the code from the address
            name = "index.html" if url.path in ("/", "/callback") else url.path.lstrip("/")
            file = (WEB / name).resolve()
            if WEB.resolve() not in file.parents or not file.is_file():
                return self._json(404, {"error": "not found"})
            self._send(200, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Requested-With") != "finances":
                return self._json(403, {"error": "forbidden"})
            path = self.path.split("?")[0]
            try:
                body = self._body()
                if path == "/api/export":
                    year = _year(str(body.get("year", "")), db, allow_all=True)
                    style = "csv" if body.get("style") == "csv" else "es"
                    kind = body.get("kind", "entries")
                    if kind not in ("entries", "summary") or (kind == "summary" and year == "all"):
                        raise BadRequest("choose entries, or a summary of one year")
                    with lock:
                        data = (yearly.export_entries if kind == "entries" else yearly.export_summary)(db, year, style)
                    return self._send(200, data, "text/csv; charset=utf-8", f"finances-{year}-{kind}.csv")
                if path == "/api/backup":
                    with lock:
                        db.save()
                        name, data = backup.make_backup_bytes()
                        backup.mark_backup()
                    return self._send(200, data, "application/zip", name)
                if path == "/api/sync":
                    inputs = {k: str(v) for k, v in (body.get("inputs") or {}).items()}
                    with sync_lock:
                        status = run_sync(
                            db, secrets, secrets.get("sources", []), inputs, lock=lock,
                            persist_secrets=lambda s: config.save_secrets(db.passphrase, s),
                            psu=self._psu())
                    return self._json(200, status)
                route = POST_ROUTES.get(path)
                if not route:
                    return self._json(404, {"error": "not found"})
                with lock:
                    return self._json(200, route(db, body))
            except (BadRequest, ValueError) as e:
                return self._json(400, {"error": str(e)})

    return Handler


def open_browser(url: str) -> None:
    """Open the dashboard: Termux has no default browser hook, Android's `am` does it."""
    import shutil
    import subprocess
    import webbrowser
    if shutil.which("am"):
        subprocess.run(["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        webbrowser.open(url)


def serve(db: Database, secrets: dict, port: int = 8765, open_it: bool = False, opener=open_browser) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db, secrets))
    print(f"Dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    if open_it:
        opener(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
