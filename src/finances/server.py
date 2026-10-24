"""Local-only JSON API + static dashboard, built on the standard library.

Bound to 127.0.0.1. To reach it from another device use a private network
(e.g. Tailscale); never expose it publicly. Mutating requests need a custom
header and the Host header is checked, to block cross-site and DNS-rebinding abuse.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analysis
from .db import Database
from .sync import run_sync

WEB = Path(__file__).parent / "web"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".svg": "image/svg+xml"}
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def make_handler(db: Database, secrets: dict, allowed_hosts=ALLOWED_HOSTS):
    lock = threading.Lock()

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

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            path = self.path.split("?")[0]
            if path == "/api/summary":
                with lock:
                    return self._json(200, analysis.summary(db))
            name = "index.html" if path == "/" else path.lstrip("/")
            file = (WEB / name).resolve()
            if WEB.resolve() not in file.parents or not file.is_file():
                return self._json(404, {"error": "not found"})
            self._send(200, file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Requested-With") != "finances":
                return self._json(403, {"error": "forbidden"})
            if self.path != "/api/sync":
                return self._json(404, {"error": "not found"})
            with lock:
                status = run_sync(db, secrets, secrets.get("sources", ["demo"]))
            self._json(200, status)

    return Handler


def serve(db: Database, secrets: dict, port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db, secrets))
    print(f"Dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
