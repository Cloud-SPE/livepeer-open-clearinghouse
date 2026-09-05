"""Database schema compatibility boundary for application readiness."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EXPECTED_ALEMBIC_REVISION = "0023"


async def current_alembic_revision(session: AsyncSession) -> str | None:
    """Return the sole applied Alembic revision, or ``None`` when unavailable."""

    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def require_compatible_schema(session: AsyncSession) -> None:
    """Refuse startup unless the database exactly matches this application build."""

    revision = await current_alembic_revision(session)
    if revision != EXPECTED_ALEMBIC_REVISION:
        raise RuntimeError(
            f"incompatible database schema: expected {EXPECTED_ALEMBIC_REVISION}, found {revision}"
        )
