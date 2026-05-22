"""Web session token generation, hashing, and signed-cookie serialization.

The session id is a 256-bit random token. We store ``sha256(token)`` in the
DB and put the raw token in an itsdangerous-signed cookie. On every request
we unseal the cookie, hash the token, and look up the matching row.

The signing layer protects against an attacker who steals a stale cookie
from logs; the DB hash protects against a database leak (a leaked hash
isn't a usable session).
"""

from __future__ import annotations

import hashlib
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "pymthouse_session"


def generate_token() -> str:
    """Return a fresh URL-safe random session token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hex-encoded sha256 of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_serializer(secret: str, salt: str = "pymthouse-session") -> URLSafeTimedSerializer:
    """Return a configured serializer keyed by `secret`."""
    return URLSafeTimedSerializer(secret, salt=salt)


def seal(serializer: URLSafeTimedSerializer, token: str) -> str:
    """Wrap a session token in a signed-with-max-age cookie value."""
    return serializer.dumps(token)


def unseal(
    serializer: URLSafeTimedSerializer, signed: str, max_age_seconds: int
) -> str | None:
    """Return the token from a signed cookie, or None if invalid/expired."""
    try:
        loaded = serializer.loads(signed, max_age=max_age_seconds)
    except BadSignature:
        return None
    if not isinstance(loaded, str):
        return None
    return loaded
