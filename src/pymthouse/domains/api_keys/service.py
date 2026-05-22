"""Business logic for api_keys."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.accounts.repo import User
from pymthouse.domains.api_keys.repo import ApiKey
from pymthouse.providers.auth import api_key as api_key_helper
from pymthouse.providers.clock import Clock


class ApiKeyServiceError(Exception):
    code = "api_key_error"


class ApiKeyNotFound(ApiKeyServiceError):
    code = "api_key_not_found"


async def create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    label: str,
    pepper: str,
) -> tuple[ApiKey, str]:
    """Create a new API key for `user_id`. Returns (row, raw_key)."""
    raw_key = api_key_helper.generate()
    prefix = api_key_helper.prefix(raw_key)
    digest = api_key_helper.hash(raw_key, pepper)

    row = ApiKey(
        user_id=user_id,
        prefix=prefix,
        hash=digest,
        label=label,
    )
    session.add(row)
    await session.flush()
    return row, raw_key


async def list_for_user(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[ApiKey]:
    """List all API keys for a user (including revoked)."""
    result = await session.scalars(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    return list(result)


async def revoke(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    clock: Clock,
) -> ApiKey:
    """Mark a user's API key revoked. Idempotent."""
    row = await session.scalar(
        select(ApiKey).where(ApiKey.id == api_key_id, ApiKey.user_id == user_id)
    )
    if row is None:
        raise ApiKeyNotFound
    if row.revoked_at is None:
        row.revoked_at = clock.now()
    return row


async def validate_raw_key(
    session: AsyncSession,
    *,
    raw_key: str,
    pepper: str,
    clock: Clock,
) -> tuple[ApiKey, User] | None:
    """Resolve a raw API key to the owning ApiKey + User, or None if invalid."""
    if not api_key_helper.looks_well_formed(raw_key):
        return None
    pfx = api_key_helper.prefix(raw_key)
    row = await session.scalar(
        select(ApiKey).where(ApiKey.prefix == pfx, ApiKey.revoked_at.is_(None))
    )
    if row is None or not api_key_helper.verify(raw_key, row.hash, pepper):
        return None
    user = await session.get(User, row.user_id)
    if user is None:
        return None
    row.last_used_at = clock.now()
    return row, user
