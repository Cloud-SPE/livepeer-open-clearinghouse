"""Password hashing with argon2id.

We use the default `PasswordHasher()` parameters (memory_cost=64MB,
time_cost=3, parallelism=4) — these are tuned for ~50–100ms per hash on
modern hardware, which is what we want for login throttling.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash(password: str) -> str:
    """Return an argon2id hash string for the given password."""
    return _hasher.hash(password)


def verify(password: str, stored_hash: str) -> bool:
    """Return True iff `password` matches `stored_hash`."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if `stored_hash` was made with weaker params than the current default."""
    return _hasher.check_needs_rehash(stored_hash)
