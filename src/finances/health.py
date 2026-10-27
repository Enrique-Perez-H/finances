"""What needs your attention: stale syncs, the bank consent running out, entries to review,
no recent backup. Shown on the Overview and as a badge on the Spending tab."""
from datetime import datetime, timezone

from . import backup
from .db import Database

CONSENT_WARN_DAYS = 14
BACKUP_WARN_DAYS = 30
STALE_SYNC_DAYS = 7


def _time(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def status(db: Database, secrets: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    accounts = [dict(r) for r in db.conn.execute(
        "SELECT name, provider, updated_at FROM accounts ORDER BY provider, name")]
    by_provider = {}
    for a in accounts:
        t = _time(a["updated_at"]) if a["updated_at"] and a["updated_at"] != "t" else None
        if t and (a["provider"] not in by_provider or t > by_provider[a["provider"]]):
            by_provider[a["provider"]] = t
    synced = [dict(provider=p, at=t.isoformat(timespec="minutes"), hours_ago=round((now - t).total_seconds() / 3600, 1))
              for p, t in sorted(by_provider.items())]

    unclear = db.conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category='uncategorized' AND paired_with=''").fetchone()[0]

    consent = None
    session = secrets.get("eb_session")
    if session and session.get("valid_until"):
        days = (_time(session["valid_until"]) - now).total_seconds() / 86400
        consent = dict(valid_until=session["valid_until"], days_left=round(days, 1))

    last = backup.last_backup()
    backup_age = round((now - _time(last)).total_seconds() / 86400, 1) if last else None

    warnings = []
    if consent and consent["days_left"] <= 0:
        warnings.append(dict(level="bad", tab=None, text="Your bank access has expired. Tap Sync and approve it again."))
    elif consent and consent["days_left"] <= CONSENT_WARN_DAYS:
        warnings.append(dict(level="warn", tab=None,
                             text=f"Your bank access expires in {int(consent['days_left'])} days. You will need to approve it again."))
    if backup_age is None:
        warnings.append(dict(level="warn", tab="accounts", text="You have never made a backup. If the phone is lost, everything is lost."))
    elif backup_age > BACKUP_WARN_DAYS:
        warnings.append(dict(level="warn", tab="accounts", text=f"Your last backup is {int(backup_age)} days old."))
    if synced and min(s["hours_ago"] for s in synced) > STALE_SYNC_DAYS * 24:
        warnings.append(dict(level="warn", tab=None, text=f"Nothing has synced for {int(min(s['hours_ago'] for s in synced) // 24)} days."))
    if unclear:
        warnings.append(dict(level="info", tab="spending", text=f"{unclear} entries still need a category."))
    return dict(synced=synced, unclear=unclear, consent=consent, backup=dict(last=last, days=backup_age),
                warnings=warnings)
