"""Backup and restore of the encrypted vault.

A backup is a zip holding the already-encrypted database and secrets files plus a manifest
with their SHA-256 hashes. Nothing is decrypted to make one, and without the passphrase it is
useless to anyone who finds it. Keep the passphrase somewhere safe: it cannot be recovered.
"""
import hashlib
import io
import json
import os
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, vault

FILES = ("finances.db.enc", "secrets.enc")
KEEP_ROLLING = 7


def _stamp_file() -> Path:
    return config.home() / "last_backup.txt"


def last_backup() -> str | None:
    try:
        return _stamp_file().read_text().strip() or None
    except OSError:
        return None


def mark_backup(now: datetime | None = None) -> None:
    _stamp_file().write_text((now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))


def make_backup_bytes(now: datetime | None = None) -> tuple[str, bytes]:
    now = now or datetime.now(timezone.utc)
    home = config.home()
    manifest = {"app": "finances", "format": 1, "created": now.isoformat(timespec="seconds"), "files": {}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # already encrypted: compressing adds nothing
        for name in FILES:
            path = home / name
            if path.exists():
                data = path.read_bytes()
                z.writestr(name, data)
                manifest["files"][name] = hashlib.sha256(data).hexdigest()
        if not manifest["files"]:
            raise ValueError("there is nothing to back up yet: sync first")
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return f"finances-backup-{now.date().isoformat()}.zip", buf.getvalue()


def write_backup(directory: str | os.PathLike, now: datetime | None = None) -> Path:
    name, data = make_backup_bytes(now)
    target = Path(directory).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    out = target / name
    out.write_bytes(data)
    out.chmod(0o600)
    mark_backup(now)
    return out


def restore_backup(path: str | os.PathLike, passphrase: str, force: bool = False) -> list[str]:
    """Put a backup back after checking it is intact and the passphrase opens it."""
    with zipfile.ZipFile(path) as z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except (KeyError, ValueError):
            raise ValueError("this is not a finances backup") from None
        files = {}
        for name, digest in manifest.get("files", {}).items():
            if name not in FILES:
                raise ValueError(f"unexpected file in backup: {name}")
            data = z.read(name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"{name} is damaged (hash mismatch); the backup is not usable")
            files[name] = data
    if "finances.db.enc" not in files:
        raise ValueError("the backup has no database")
    vault.decrypt(passphrase, files["finances.db.enc"])  # raises WrongPassphrase if it does not open
    if "secrets.enc" in files:
        vault.decrypt(passphrase, files["secrets.enc"])
    home = config.home()
    existing = [n for n in files if (home / n).exists()]
    if existing and not force:
        raise ValueError("a vault already exists here; use --force to replace it (the current one is kept aside)")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for name in existing:
        (home / name).replace(home / f"{name}.before-restore-{stamp}")
    for name, data in files.items():
        tmp = home / f"{name}.tmp"
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        tmp.replace(home / name)
    return list(files)


def rolling_copy(today: date | None = None) -> None:
    """Keep one copy of the database per day (last 7), protection against corruption or a slip."""
    path = config.db_path()
    if not path.exists():
        return
    folder = config.home() / "backups"
    folder.mkdir(exist_ok=True, mode=0o700)
    target = folder / f"db-{(today or date.today()).isoformat()}.enc"
    if not target.exists():
        target.write_bytes(path.read_bytes())
        target.chmod(0o600)
    for old in sorted(folder.glob("db-*.enc"))[:-KEEP_ROLLING]:
        old.unlink()
