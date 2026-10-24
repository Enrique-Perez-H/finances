import argparse
import json
import os
import sys

from . import config, vault
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
    forget = sub.add_parser("forget", help="delete all stored data of one provider (e.g. demo)")
    forget.add_argument("provider")
    sf = sub.add_parser("secret-file", help="store a file's contents (e.g. a .pem key) as a secret")
    sf.add_argument("key")
    sf.add_argument("path")
    sec = sub.add_parser("secret", help="store a secret in the encrypted vault")
    sec.add_argument("key")
    args = p.parse_args(argv)

    if args.demo:
        os.environ.setdefault("FINANCES_HOME", os.path.expanduser("~/.finances-demo"))
    try:
        return _run(args)
    except vault.WrongPassphrase:
        print("Wrong passphrase. (If this is a throwaway demo vault you can delete it: "
              f"rm -rf {config.home()})", file=sys.stderr)
        return 1


def _sync_interactive(db, secrets, sources, passphrase):
    """Run a sync, asking on the terminal whenever a connector needs a code."""
    import getpass
    save = lambda s: config.save_secrets(passphrase, s)  # noqa: E731
    status = run_sync(db, secrets, sources, persist_secrets=save)
    for name, st in list(status.items()):
        while st["state"] == "needs_input" and st.get("field"):
            code = getpass.getpass(f"{name}: {st['prompt']}: ")
            st = run_sync(db, secrets, [name], {name: code}, persist_secrets=save)[name]
            status[name] = st
    return status


def _run(args):
    passphrase = config.get_passphrase()
    secrets = config.load_secrets(passphrase)

    if args.cmd == "secret-file":
        with open(os.path.expanduser(args.path)) as f:
            secrets[args.key] = f.read()
        config.save_secrets(passphrase, secrets)
        print(f"Saved. You can now delete {args.path}.")
        return 0

    if args.cmd == "secret":
        import getpass
        value = getpass.getpass(f"Value for {args.key} (hidden): ")
        # Only JSON lists/objects are parsed (e.g. sources); everything else stays a string,
        # so a PIN like 1234 is not turned into a number.
        if value.lstrip().startswith(("[", "{")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        secrets[args.key] = value
        config.save_secrets(passphrase, secrets)
        print("Saved.")
        return 0

    db = Database(passphrase)
    sources = ["demo"] if args.demo else secrets.get("sources", [])
    if args.demo:
        secrets = {**secrets, "sources": sources}
    if args.cmd == "forget":
        n = db.delete_provider(args.provider)
        db.save()
        print(f"Removed {n} account(s) of '{args.provider}'.")
    elif args.cmd == "sync":
        print(json.dumps(_sync_interactive(db, secrets, sources, passphrase), indent=2))
    elif args.cmd == "serve":
        if not db.conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() and sources:
            run_sync(db, secrets, sources,
                     persist_secrets=lambda s: config.save_secrets(passphrase, s))
        serve(db, secrets, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
