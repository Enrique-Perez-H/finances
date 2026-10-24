import contextlib

from .categorize import categorize_all
from .connectors import REGISTRY
from .connectors.base import ConnectorError, NeedsInput, Skipped
from .db import Database

# Connectors that raised NeedsInput stay alive here so the next call can continue
# the same login instead of starting over.
_pending: dict = {}


def run_sync(db: Database, secrets: dict, names: list[str], inputs: dict | None = None,
             lock=None, persist_secrets=None, psu: dict | None = None) -> dict:
    """Run the named connectors and return per-connector status for the UI.

    `lock` guards only the database writes, so slow network calls (a login waiting
    to be confirmed in an app) don't freeze the dashboard. `persist_secrets` is
    called with the secrets dict if a connector changed it (e.g. a new session).
    """
    inputs = inputs or {}
    lock = lock or contextlib.nullcontext()
    before = repr(secrets)
    status = {}
    for name in names:
        connector = _pending.pop(name, None) or REGISTRY[name](secrets)
        connector.set_psu(psu)
        try:
            snap = connector.fetch(user_input=inputs.get(name))
        except NeedsInput as e:
            _pending[name] = connector
            status[name] = {"state": "needs_input", "prompt": e.prompt, "url": e.url,
                            "field": e.field}
            continue
        except Skipped as e:
            status[name] = {"state": "skipped", "note": str(e)}
            continue
        except NotImplementedError as e:
            status[name] = {"state": "unavailable", "error": str(e)}
            continue
        except Exception as e:  # keep one failing provider from blocking the rest
            # Only a type name or the connector's own message: never raw responses.
            msg = str(e) if isinstance(e, (ValueError, RuntimeError, ConnectorError)) else ""
            status[name] = {"state": "error", "error": f"{type(e).__name__}: {msg}".strip(": ")}
            continue
        with lock:
            for a in snap.accounts:
                db.upsert_account(a)
            for t in snap.transactions:
                db.upsert_transaction(t)
            for account_id, rows in snap.holdings.items():
                db.replace_holdings(account_id, rows)
            categorize_all(db)
            db.save()
        status[name] = {"state": "ok", "accounts": len(snap.accounts),
                        "transactions": len(snap.transactions)}
    if persist_secrets and repr(secrets) != before:
        persist_secrets(secrets)
    return status
