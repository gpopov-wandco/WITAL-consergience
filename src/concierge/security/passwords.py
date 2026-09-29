"""Argon2id password hashing.

Implements: F19, F31
"""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, type=Type.ID)

MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    pass


def check_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"password must have at least {MIN_PASSWORD_LENGTH} characters")
    if password.strip() != password:
        raise WeakPasswordError("password must not start or end with whitespace")


def hash_password(password: str) -> str:
    check_strength(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_hasher.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return bool(_hasher.check_needs_rehash(password_hash))
    except InvalidHashError:
        return True
