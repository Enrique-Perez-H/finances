"""Locations and secret handling. Nothing here is ever stored inside the repo."""
import getpass
import json
import os
from pathlib import Path

from . import vault


def home() -> Path:
    path = Path(os.environ.get("FINANCES_HOME", Path.home() / ".finances"))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def db_path() -> Path:
    return home() / "finances.db.enc"


def secrets_path() -> Path:
    return home() / "secrets.enc"


def get_passphrase() -> str:
    env = os.environ.get("FINANCES_PASSPHRASE")
    if env:
        return env
    if db_path().exists() or secrets_path().exists():
        return getpass.getpass("Vault passphrase: ")
    # New vault: ask twice, since a typo here would lock you out of your own data.
    while True:
        first = getpass.getpass("Choose a vault passphrase: ")
        if first and first == getpass.getpass("Repeat passphrase: "):
            return first
        print("Passphrases did not match (or were empty), try again.")


def load_secrets(passphrase: str) -> dict:
    path = secrets_path()
    if not path.exists():
        return {}
    return json.loads(vault.decrypt(passphrase, path.read_bytes()))


def save_secrets(passphrase: str, secrets: dict) -> None:
    path = secrets_path()
    path.write_bytes(vault.encrypt(passphrase, json.dumps(secrets).encode()))
    path.chmod(0o600)
