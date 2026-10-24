from .connectors import REGISTRY
from .connectors.base import NeedsInput
from .db import Database


def run_sync(db: Database, secrets: dict, names: list[str]) -> dict:
    """Run the named connectors. Returns per-connector status for the UI."""
    status = {}
    for name in names:
        try:
            snap = REGISTRY[name](secrets).fetch()
        except NeedsInput as e:
            status[name] = {"state": "needs_input", "prompt": e.prompt, "url": e.url}
            continue
        except NotImplementedError as e:
            status[name] = {"state": "unavailable", "error": str(e)}
            continue
        except Exception as e:  # keep one failing provider from blocking the rest
            status[name] = {"state": "error", "error": type(e).__name__}
            continue
        for a in snap.accounts:
            db.upsert_account(a)
        for t in snap.transactions:
            db.upsert_transaction(t)
        for account_id, rows in snap.holdings.items():
            db.replace_holdings(account_id, rows)
        status[name] = {"state": "ok", "accounts": len(snap.accounts),
                        "transactions": len(snap.transactions)}
    db.save()
    return status
