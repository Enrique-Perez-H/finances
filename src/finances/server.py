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

from . import analysis, categorize, config, forecast, fund, income, recurring
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
    "/api/spending": lambda db, q: analysis.spending(db, _q(q, "month")),
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
    "/api/review": lambda db, q: categorize.review_queue(db),
    "/api/review/generic": lambda db, q: categorize.generic_entries(db),
    "/api/categories": lambda db, q: categorize.all_categories(db),
    "/api/rules": lambda db, q: [dict(r) for r in db.conn.execute(
        "SELECT pattern, category FROM rules ORDER BY pattern")],
}


def _post_categorize(db, body):
    category = str(body.get("category", "")).strip()
    if "tx_id" in body:  # one entry only; never overwritten by rules afterwards
        categorize.set_manual(db, str(body["tx_id"]), category)
        changed = 1
    else:
        changed = categorize.add_rule(db, str(body.get("pattern", "")), category)
    db.save()
    return {"changed": changed}


def _post_delete_rule(db, body):
    changed = categorize.delete_rule(db, str(body.get("pattern", "")))
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


# POST routes: (db, body) -> JSON-able. These change local data only.
POST_ROUTES = {
    "/api/funds": _post_fund_save,
    "/api/funds/delete": _post_fund_delete,
    "/api/funds/preview": lambda db, body: fund.preview(db, body),
    "/api/categorize": _post_categorize,
    "/api/rules/delete": _post_delete_rule,
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

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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


def serve(db: Database, secrets: dict, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db, secrets))
    print(f"Dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
