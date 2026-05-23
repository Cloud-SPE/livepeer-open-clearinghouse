"""API key generation, prefix derivation, and constant-time hash verification.

The raw key has the shape ``pymth_live_<32 url-safe chars>``. The first
``KEY_PREFIX_LEN`` characters of the full key form the unique `prefix`
that's stored in plaintext for dashboard display and DB lookup. The hash
is ``sha256(pepper || raw_key)`` stored hex-encoded.

Lookup at request time:
    1. Extract `prefix` from the raw key
    2. SELECT api_key WHERE prefix = ? AND revoked_at IS NULL
    3. Compute candidate hash and `hmac.compare_digest` against stored hash
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_BRAND = "pymth_live_"
KEY_RANDOM_LEN = 32  # url-safe characters appended after the brand
KEY_PREFIX_LEN = len(KEY_BRAND) + 8  # brand + 8 chars is enough to be unique


def generate() -> str:
    """Generate a new raw API key. Show to the user once; never store it raw."""
    suffix = secrets.token_urlsafe(KEY_RANDOM_LEN)[:KEY_RANDOM_LEN]
    return KEY_BRAND + suffix


def prefix(raw_key: str) -> str:
    """Return the public-displayable prefix for a raw key."""
    return raw_key[:KEY_PREFIX_LEN]


def hash(raw_key: str, pepper: str) -> str:
    """Return the hex-encoded sha256(pepper || raw_key)."""
    digest = hashlib.sha256()
    digest.update(pepper.encode("utf-8"))
    digest.update(raw_key.encode("utf-8"))
    return digest.hexdigest()


def verify(raw_key: str, stored_hash: str, pepper: str) -> bool:
    """Constant-time comparison of `hash(raw_key, pepper)` with `stored_hash`."""
    candidate = hash(raw_key, pepper)
    return hmac.compare_digest(candidate, stored_hash)


def looks_well_formed(raw_key: str) -> bool:
    """Cheap shape check before hitting the DB. Not a security boundary."""
    return raw_key.startswith(KEY_BRAND) and len(raw_key) >= KEY_PREFIX_LEN
