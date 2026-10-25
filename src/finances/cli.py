import argparse
import json
import os
import sys

from . import config, vault
from .db import Database
from .server import local_ip, serve
from .sync import run_sync


def main(argv=None):
    p = argparse.ArgumentParser(prog="finances")
    p.add_argument("--demo", action="store_true",
                   help="use a separate demo vault (~/.finances-demo) with fake data")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="fetch data from the configured sources")
    s = sub.add_parser("serve", help="run the dashboard on localhost")
    s.add_argument("--port", type=int, default=8765)
    fd = sub.add_parser("find", help="search your entries by text (shown only in your terminal)")
    fd.add_argument("text")
    rl = sub.add_parser("rules", help="list your own category rules (or delete one)")
    rl.add_argument("--delete", metavar="PATTERN")
    sub.add_parser("oddities", help="entries whose category does not fit their sign or size")
    mg = sub.add_parser("merge", help="merge a category into another (also renames)")
    mg.add_argument("src")
    mg.add_argument("dst")
    sub.add_parser("recategorize", help="apply the current rules and category names to every entry")
    sub.add_parser("categories", help="every category with how many entries and how much it holds")
    um = sub.add_parser("unmatched", help="most common entries that could not be categorised")
    um.add_argument("--top", type=int, default=30)
    ff = sub.add_parser("fields", help="which fields the bank fills in (counts only, no values)")
    ff.add_argument("--live", action="store_true", help="ask the bank again (uses up daily reads)")
    sub.add_parser("status", help="show which secrets/sources are set (never their values)")
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
    psu = {"Psu-Ip-Address": secrets.get("eb_psu_ip") or local_ip(), "Psu-User-Agent": "finances-cli"}
    status = run_sync(db, secrets, sources, persist_secrets=save, psu=psu)
    for name, st in list(status.items()):
        while st["state"] == "needs_input" and st.get("field"):
            code = getpass.getpass(f"{name}: {st['prompt']}: ")
            st = run_sync(db, secrets, [name], {name: code}, persist_secrets=save, psu=psu)[name]
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

    if args.cmd == "status":
        keys = sorted(k for k in secrets if not k.startswith("_"))
        print("Secrets stored:", ", ".join(keys) or "none")
        print("Sources:", secrets.get("sources") or "none set (run: finances secret sources)")
        session = secrets.get("eb_session")
        if session:
            print(f"Bank consent valid until {session['valid_until']}, "
                  f"{len(session['accounts'])} account(s)")
        for k in ("eb_app_id", "eb_private_key", "tr_phone", "tr_pin"):
            print(f"  {k}: {'set' if secrets.get(k) else 'MISSING'}")
        if secrets.get("eb_private_key", "").startswith("-----BEGIN") is False and secrets.get("eb_private_key"):
            print("  WARNING: eb_private_key does not look like a PEM key")
        if secrets.get("eb_app_id"):
            print(f"  eb_app_id length {len(secrets['eb_app_id'])} (a UUID has 36)")
        db = Database(passphrase)
        for r in db.conn.execute("SELECT provider, COUNT(*) n, MAX(updated_at) last FROM accounts GROUP BY provider"):
            print(f"Data: {r['provider']}: {r['n']} account(s), last update {r['last']}")
        return 0

    if args.cmd == "rules":
        from . import categorize
        db = Database(passphrase)
        if args.delete:
            print(f"{categorize.delete_rule(db, args.delete)} entries changed.")
            db.save()
            return 0
        for r in db.conn.execute("SELECT pattern, direction, category FROM rules ORDER BY category, pattern"):
            flag = "  (ignored: too generic)" if r["pattern"] in categorize.GENERIC_PATTERNS else ""
            print(f"{r['category']:<24} <- \"{r['pattern']}\" [{r['direction']}]{flag}")
        return 0
    if args.cmd == "merge":
        from . import categorize
        db = Database(passphrase)
        print(f"{categorize.merge_category(db, args.src, args.dst)} entries moved.")
        db.save()
        return 0
    if args.cmd == "oddities":
        from . import categorize
        db = Database(passphrase)
        income = set(categorize.INCOME_CATEGORIES)
        special = {categorize.INTERNAL, categorize.UNCATEGORIZED}
        print("Money IN under a spending category (biggest first):")
        for r in db.conn.execute(
                "SELECT date, amount, category, cat_source, description FROM transactions"
                " WHERE amount > 100 AND paired_with='' ORDER BY amount DESC"):
            if r["category"] not in income and r["category"] not in special:
                print(f"{r['date']} {r['amount']:>10.2f}  {r['category']:<18} {r['cat_source'] or 'auto':<9} "
                      f"{r['description'][:60]}")
        print("Biggest money OUT (any category):")
        for r in db.conn.execute(
                "SELECT date, amount, category, cat_source, description FROM transactions"
                " WHERE amount < -1000 AND paired_with='' ORDER BY amount LIMIT 15"):
            print(f"{r['date']} {r['amount']:>10.2f}  {r['category']:<18} {r['cat_source'] or 'auto':<9} "
                  f"{r['description'][:60]}")
        return 0
    if args.cmd == "recategorize":
        from . import categorize
        db = Database(passphrase)
        print(f"{categorize.categorize_all(db)} entries changed.")
        db.save()
        return 0
    if args.cmd == "categories":
        from . import categorize
        db = Database(passphrase)
        stats = {r["category"]: (r["n"], r["spent"], r["received"]) for r in db.conn.execute(
            "SELECT category, COUNT(*) n, SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) spent,"
            " SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) received FROM transactions GROUP BY category")}
        for name in categorize.all_categories(db) + [categorize.UNCATEGORIZED]:
            n, spent, received = stats.get(name, (0, 0, 0))
            print(f"{name:<24} {n:>5} entries  out {spent or 0:>10.2f}  in {received or 0:>10.2f}")
        return 0
    if args.cmd in ("find", "unmatched"):
        from . import categorize
        db = Database(passphrase)
        if args.cmd == "find":
            needle = categorize.normalize(args.text)
            n = 0
            for r in db.conn.execute("SELECT date, amount, category, description, mcc FROM transactions"
                                     " ORDER BY date DESC"):
                if needle in categorize.normalize(r["description"]):
                    n += 1
                    if n <= 40:
                        print(f"{r['date']} {r['amount']:>10.2f}  {r['category']:<20} "
                              f"mcc={r['mcc'] or '-':<5} {r['description'][:70]}")
            print(f"{n} match(es)")
        else:
            for g in categorize.review_queue(db, args.top):
                print(f"x{g['count']:<3} {g['total']:>10.2f}  {g['key'][:50]}")
        return 0
    if args.cmd == "fields":
        from .connectors.enable_banking import EnableBankingConnector
        report = secrets.get("eb_fields")
        if args.live or not report:
            report = EnableBankingConnector(secrets).field_report()
            print(f"{report['transactions']} transactions in the last 45 days (live). "
                  "Fields with a value (count):")
        else:
            print(f"{report['transactions']} transactions seen at the last sync ({report['at']}). "
                  "Fields with a value (count):")
        for name, n in report["fields"].items():
            print(f"  {name}: {n}")
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
        from . import categorize
        categorize.categorize_all(db)  # picks up renamed categories and new rules right away
        db.save()
        if not db.conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() and sources:
            run_sync(db, secrets, sources,
                     persist_secrets=lambda s: config.save_secrets(passphrase, s))
        serve(db, secrets, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
