"""Time-ordered identifiers.

Implements: F31

Identifiers are ULIDs (48-bit millisecond timestamp plus 80 random bits, Crockford
base32) with a short type prefix, for example ``conv_01j8...``. They sort by creation
time, carry no guest data and are unguessable.
"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def ulid(timestamp_ms: int | None = None) -> str:
    """A 26-character lowercase ULID."""
    ts = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if ts < 0 or ts >= 1 << 48:
        raise ValueError("timestamp out of range")
    value = (ts << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """A prefixed identifier such as ``msg_01j8zr...``."""
    if not prefix.isalnum():
        raise ValueError("prefix must be alphanumeric")
    return f"{prefix}_{ulid()}"


def token(nbytes: int = 32) -> str:
    """A URL-safe random token (256 bits by default)."""
    return secrets.token_urlsafe(nbytes)
