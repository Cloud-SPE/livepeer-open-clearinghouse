"""Async SQLAlchemy engine, sessionmaker, and FastAPI dependency.

All three accessors below are parameter-free on purpose: they read from
the lru-cached ``get_settings()`` singleton. We deliberately don't accept
a Settings argument because Pydantic v2 ``BaseSettings`` instances aren't
hashable and would break ``@lru_cache`` keying. If a test needs to point
at a different DATABASE_URL, override the env via ``pytest.MonkeyPatch``
and clear ``get_settings.cache_clear()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pymthouse.settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the process-wide async engine."""
    cfg = get_settings()
    return create_async_engine(
        cfg.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async sessionmaker."""
    return async_sessionmaker(
        get_engine(),
        expire_on_commit=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context-managed AsyncSession with commit/rollback handling."""
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for a per-request AsyncSession."""
    async with session_scope() as session:
        yield session
