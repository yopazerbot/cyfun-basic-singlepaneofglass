"""Encryption of secrets stored on the Settings page.

AES-256-GCM with a key derived (HKDF-SHA256) from CYFUN_SECRET_KEY. Every value gets a
random 96-bit nonce, and the setting name is bound as associated data, so a ciphertext
copied into another setting does not decrypt. The stored key id tells a wrong or changed
server key apart from a damaged value.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import Settings


class SecretBoxError(Exception):
    pass


def _key(settings: Settings) -> bytes:
    problem = settings.secret_key_problem
    if problem:
        raise SecretBoxError(problem)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"cyfun-settings-v1", info=b"aes-256-gcm")
    return hkdf.derive(settings.secret_key.strip().encode("utf-8"))


def key_id(settings: Settings) -> str:
    """Short identifier of the current server key; not usable to recover the key."""
    return hashlib.sha256(b"cyfun-key-id" + _key(settings)).hexdigest()[:12]


def _aad(name: str) -> bytes:
    return f"cyfun-setting:{name}".encode()


def encrypt(settings: Settings, name: str, plaintext: str) -> tuple[str, str]:
    """Returns (token, key id)."""
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key(settings)).encrypt(nonce, plaintext.encode("utf-8"), _aad(name))
    return base64.b64encode(nonce + ct).decode("ascii"), key_id(settings)


def decrypt(settings: Settings, name: str, token: str, stored_key_id: str = "") -> str:
    if stored_key_id and stored_key_id != key_id(settings):
        raise SecretBoxError("stored with a different CYFUN_SECRET_KEY")
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True)
        return AESGCM(_key(settings)).decrypt(raw[:12], raw[12:], _aad(name)).decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise SecretBoxError("cannot be decrypted") from exc
