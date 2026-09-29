"""Server-side session tokens and CSRF tokens.

Implements: F19, F31

A session token is 256 random bits; only its SHA-256 is stored. The CSRF token is an
HMAC of the stored session hash under the signing key, so it needs no extra storage
and cannot be reused across sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

SESSION_COOKIE = "gc_session"
SESSION_TTL = timedelta(hours=12)
SESSION_IDLE_TTL = timedelta(hours=2)
MAGIC_LINK_TTL = timedelta(minutes=15)
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15


def new_session_token() -> tuple[str, str]:
    """Return (token for the cookie, hash for the database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(session_hash: str, signing_key: bytes) -> str:
    return hmac.new(
        signing_key, b"csrf:" + session_hash.encode("ascii"), hashlib.sha256
    ).hexdigest()


def csrf_ok(session_hash: str, signing_key: bytes, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(session_hash, signing_key), presented.strip())


def cookie_kwargs(secure: bool) -> dict[str, object]:
    return {
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": int(SESSION_TTL.total_seconds()),
    }
