"""Find-or-link an account from an OAuth callback.

Resolution order:
    1. ``(provider, provider_user_id)`` match -> return the linked User.
    2. ``email`` match against an existing User -> link a new identity row
       and return that User. (Email-verified by the provider; if the
       existing user hadn't verified yet, flip ``email_verified_at`` now.)
    3. Otherwise -> create a fresh User with ``email_verified_at = now``,
       attach the identity, return.

The caller is expected to have already validated that the provider
attested to the email (``email_verified=True``). We refuse to create
accounts from unverified provider emails.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.accounts.repo import User, UserOAuthIdentity
from livepeer_open_clearinghouse.providers.clock import Clock


class OAuthServiceError(Exception):
    code = "oauth_error"


class UnverifiedProviderEmail(OAuthServiceError):
    code = "oauth_email_unverified"


async def find_or_link_user(
    session: AsyncSession,
    *,
    provider: str,
    provider_user_id: str,
    email: str,
    email_verified: bool,
    clock: Clock,
) -> User:
    if not email_verified:
        raise UnverifiedProviderEmail

    # 1. By (provider, provider_user_id) — the stable, primary key.
    identity = await session.scalar(
        select(UserOAuthIdentity).where(
            UserOAuthIdentity.provider == provider,
            UserOAuthIdentity.provider_user_id == provider_user_id,
        )
    )
    if identity is not None:
        user = await session.get(User, identity.user_id)
        if user is not None:
            return user

    # 2. By email — link an additional identity to an existing local account.
    existing: User | None = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        now = clock.now()
        if existing.email_verified_at is None:
            existing.email_verified_at = now
        session.add(
            UserOAuthIdentity(
                user_id=existing.id,
                provider=provider,
                provider_user_id=provider_user_id,
                email_at_link=email,
            )
        )
        await session.flush()
        return existing

    # 3. Fresh account.
    now = clock.now()
    user = User(email=email, email_verified_at=now, password_hash=None)
    session.add(user)
    await session.flush()
    session.add(
        UserOAuthIdentity(
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email_at_link=email,
        )
    )
    await session.flush()
    return user
