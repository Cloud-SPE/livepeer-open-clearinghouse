"""Business logic for accounts.

All functions take an `AsyncSession` plus their inputs and return typed
domain objects. No HTTP, no JSON. The runtime layer composes these.

Errors are raised as `ServiceError` subclasses; runtime translates them
into HTTP responses.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.accounts.repo import (
    OperatorApproval,
    User,
    UserEmailVerification,
    UserPasswordReset,
    UserSession,
)
from pymthouse.providers.auth import password as pwd
from pymthouse.providers.auth import session as session_helper
from pymthouse.providers.clock import Clock
from pymthouse.providers.email import EmailProvider, templates

EMAIL_VERIFICATION_TTL = timedelta(hours=24)
PASSWORD_RESET_TTL = timedelta(hours=1)
SESSION_TTL = timedelta(days=14)


class ServiceError(Exception):
    """Base class for service-level errors with a stable error code."""

    code = "service_error"


class EmailAlreadyRegistered(ServiceError):
    code = "email_already_registered"


class InvalidCredentials(ServiceError):
    code = "invalid_credentials"


class InvalidToken(ServiceError):
    code = "invalid_token"


class EmailNotVerified(ServiceError):
    code = "email_not_verified"


# ---------------------------------------------------------------------------
# Signup + email verification
# ---------------------------------------------------------------------------


async def send_verification_email(
    session: AsyncSession,
    *,
    user: User,
    clock: Clock,
    email_provider: EmailProvider,
    public_base_url: str,
) -> None:
    """Mint a fresh verification token for `user` and send the email.

    Previous (unconsumed) tokens for this user remain valid until their
    own TTL expires — multiple in-flight tokens are fine, the verify
    endpoint takes whichever one the user actually clicks.
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session.add(
        UserEmailVerification(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=clock.now() + EMAIL_VERIFICATION_TTL,
        )
    )
    await session.flush()

    verify_link = (
        f"{public_base_url.rstrip('/')}/portal/#/verify-email?token={raw_token}"
    )
    await email_provider.send(
        templates.verification_email(to=user.email, verify_link=verify_link)
    )


async def signup(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    clock: Clock,
    email_provider: EmailProvider,
    public_base_url: str,
) -> User:
    """Create a new user and send the email-verification message."""
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise EmailAlreadyRegistered

    user = User(
        email=email,
        password_hash=pwd.hash(password),
    )
    session.add(user)
    await session.flush()
    await send_verification_email(
        session,
        user=user,
        clock=clock,
        email_provider=email_provider,
        public_base_url=public_base_url,
    )
    return user


async def verify_email(
    session: AsyncSession, *, token: str, clock: Clock
) -> User:
    """Consume a verification token; flips `email_verified_at`."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    verification = await session.scalar(
        select(UserEmailVerification).where(
            UserEmailVerification.token_hash == token_hash,
            UserEmailVerification.consumed_at.is_(None),
        )
    )
    now = clock.now()
    if verification is None or verification.expires_at < now:
        raise InvalidToken
    verification.consumed_at = now

    user = await session.get(User, verification.user_id)
    if user is None:
        raise InvalidToken
    user.email_verified_at = now
    return user


# ---------------------------------------------------------------------------
# Login + session
# ---------------------------------------------------------------------------


async def login(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    clock: Clock,
) -> tuple[User, str]:
    """Verify credentials and create a session. Returns (user, raw_session_token)."""
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or user.password_hash is None:
        raise InvalidCredentials
    if not pwd.verify(password, user.password_hash):
        raise InvalidCredentials
    if user.email_verified_at is None:
        raise EmailNotVerified

    raw_token = session_helper.generate_token()
    token_hash = session_helper.hash_token(raw_token)
    ws = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=clock.now() + SESSION_TTL,
    )
    session.add(ws)
    return user, raw_token


async def resolve_session(
    session: AsyncSession, *, raw_token: str, clock: Clock
) -> User | None:
    """Return the user behind a session token, or None if invalid/expired/revoked."""
    token_hash = session_helper.hash_token(raw_token)
    ws = await session.scalar(
        select(UserSession).where(UserSession.token_hash == token_hash)
    )
    if ws is None or ws.revoked_at is not None or ws.expires_at < clock.now():
        return None
    return await session.get(User, ws.user_id)


async def revoke_session(
    session: AsyncSession, *, raw_token: str, clock: Clock
) -> None:
    """Idempotent: mark a session revoked."""
    token_hash = session_helper.hash_token(raw_token)
    ws = await session.scalar(
        select(UserSession).where(UserSession.token_hash == token_hash)
    )
    if ws is None or ws.revoked_at is not None:
        return
    ws.revoked_at = clock.now()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def is_approved(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """True iff user has an active (non-revoked) operator approval."""
    approval = await session.scalar(
        select(OperatorApproval).where(
            OperatorApproval.user_id == user_id,
            OperatorApproval.revoked_at.is_(None),
        )
    )
    return approval is not None


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


async def request_password_reset(
    session: AsyncSession,
    *,
    email: str,
    clock: Clock,
    email_provider: EmailProvider,
    public_base_url: str,
) -> None:
    """Initiate a reset for `email`. Silent no-op when no user exists.

    The endpoint always succeeds from the caller's perspective; we don't
    leak whether the address is registered.
    """
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or user.password_hash is None:
        # Either the email doesn't belong to a user, or the user is
        # OAuth-only and has no password to reset.
        return

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session.add(
        UserPasswordReset(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=clock.now() + PASSWORD_RESET_TTL,
        )
    )
    await session.flush()

    reset_link = (
        f"{public_base_url.rstrip('/')}/portal/#/reset-password?token={raw_token}"
    )
    await email_provider.send(
        templates.password_reset_email(
            to=user.email,
            reset_link=reset_link,
            ttl_minutes=int(PASSWORD_RESET_TTL.total_seconds() // 60),
        )
    )


async def reset_password(
    session: AsyncSession,
    *,
    token: str,
    new_password: str,
    clock: Clock,
) -> User:
    """Consume the reset token, set the new password, revoke every active session."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = await session.scalar(
        select(UserPasswordReset).where(
            UserPasswordReset.token_hash == token_hash,
            UserPasswordReset.consumed_at.is_(None),
        )
    )
    now = clock.now()
    if record is None or record.expires_at < now:
        raise InvalidToken
    record.consumed_at = now

    user = await session.get(User, record.user_id)
    if user is None:
        raise InvalidToken
    user.password_hash = pwd.hash(new_password)

    # Invalidate every still-active session for this user. The reset
    # implies the previous credentials were compromised (or the user
    # was locked out); leaving old sessions live would defeat the point.
    sessions = await session.scalars(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    )
    for s in sessions:
        s.revoked_at = now

    return user
