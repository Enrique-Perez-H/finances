"""Passphrase-based authenticated encryption for the database and secrets.

Format: MAGIC | salt(16) | nonce(12) | AES-256-GCM ciphertext+tag.
The key is derived with scrypt, so a stolen file is only as weak as the passphrase.
"""
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"FINV1"
_SALT_LEN = 16
_NONCE_LEN = 12
_SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


class WrongPassphrase(Exception):
    pass


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, **_SCRYPT)


def encrypt(passphrase: str, plaintext: bytes) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_derive(passphrase, salt)).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + salt + nonce + ct


def decrypt(passphrase: str, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not a vault file")
    head = len(MAGIC)
    salt = blob[head : head + _SALT_LEN]
    nonce = blob[head + _SALT_LEN : head + _SALT_LEN + _NONCE_LEN]
    ct = blob[head + _SALT_LEN + _NONCE_LEN :]
    try:
        return AESGCM(_derive(passphrase, salt)).decrypt(nonce, ct, MAGIC)
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase or corrupted file") from None
