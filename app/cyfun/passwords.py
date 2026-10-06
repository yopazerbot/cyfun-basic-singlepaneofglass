"""Password hashing for local accounts: scrypt from the standard library, random salt per password.

Stored format: scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import string

N, R, P, DKLEN = 2**14, 8, 2, 32  # 16 MiB memory, two parallel lanes; well under hashlib's default memory cap
MIN_LENGTH = 12
WEAK = {"admin", "password", "passw0rd", "cyfun", "welcome", "changeme", "letmein", "qwerty", "123456789012", "administrator"}


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def password_problems(password: str, username: str = "") -> list[str]:
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > 200:
        problems.append("Use at most 200 characters.")
    low = password.lower()
    if username and username.lower() in low:
        problems.append("The password must not contain the username.")
    if low in WEAK or low.rstrip("0123456789!") in WEAK:
        problems.append("That password is on the list of common passwords.")
    return problems


def temporary_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
