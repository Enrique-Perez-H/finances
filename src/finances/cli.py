import argparse
import json
import os
import sys

from . import config
from .db import Database
from .server import serve
from .sync import run_sync


def main(argv=None):
    p = argparse.ArgumentParser(prog="finances")
    p.add_argument("--demo", action="store_true",
                   help="use a separate demo vault (~/.finances-demo) with fake data")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="fetch data from the configured sources")
    s = sub.add_parser("serve", help="run the dashboard on localhost")
    s.add_argument("--port", type=int, default=8765)
    sec = sub.add_parser("secret", help="store a secret in the encrypted vault")
    sec.add_argument("key")
    args = p.parse_args(argv)

    if args.demo:
        os.environ.setdefault("FINANCES_HOME", os.path.expanduser("~/.finances-demo"))
    passphrase = config.get_passphrase()
    secrets = config.load_secrets(passphrase)

    if args.cmd == "secret":
        import getpass
        value = getpass.getpass(f"Value for {args.key} (hidden): ")
        try:
            secrets[args.key] = json.loads(value)  # allows lists, e.g. sources
        except json.JSONDecodeError:
            secrets[args.key] = value
        config.save_secrets(passphrase, secrets)
        print("Saved.")
        return 0

    db = Database(passphrase)
    sources = ["demo"] if args.demo else secrets.get("sources", [])
    if args.demo:
        secrets = {**secrets, "sources": sources}
    if args.cmd == "sync":
        print(json.dumps(run_sync(db, secrets, sources), indent=2))
    elif args.cmd == "serve":
        if not db.conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() and sources:
            run_sync(db, secrets, sources)
        serve(db, secrets, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
