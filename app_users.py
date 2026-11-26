"""Guest accounts: APP_USERS on the host, "name:hash" entries apart by ";" or a new line.

A guest logs in like the main account (APP_USERNAME / APP_PASSWORD) and uses every page but
Pengaturan, which main refuses them. Their passwords are never stored as typed: each entry holds
a PBKDF2-SHA256 hash with its own salt (hash_password), made on the owner's machine with
tools/hash_password.py, so the host's environment does not give the passwords away.
"""
import hashlib
import hmac
import os
import re
import secrets
from typing import Dict, Optional

_ITERATIONS = 200_000
_SCHEME = "pbkdf2_sha256"
MIN_LENGTH = 12


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    """'pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>' for `password`, a fresh salt each time."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_SCHEME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` is the one `stored` was made from; False for a malformed entry."""
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != _SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def guest_users() -> Dict[str, str]:
    """Guest name → stored hash, from APP_USERS."""
    users: Dict[str, str] = {}
    for entry in re.split(r"[;\n]", os.getenv("APP_USERS", "")):
        name, sep, stored = entry.strip().partition(":")
        if sep and name.strip() and stored.strip():
            users[name.strip()] = stored.strip()
    return users


def password_problem(password: str) -> Optional[str]:
    """What a guest password lacks (at least 12 characters, letters and digits), or None."""
    if len(password) < MIN_LENGTH:
        return f"Password minimal {MIN_LENGTH} karakter."
    if not re.search(r"[A-Za-z]", password):
        return "Password harus memuat huruf."
    if not re.search(r"\d", password):
        return "Password harus memuat angka."
    return None
