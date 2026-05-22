"""Async SQLAlchemy engine, sessionmaker, and FastAPI dependency."""

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

from pymthouse.settings import Settings, get_settings


@lru_cache(maxsize=1)
def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine."""
    cfg = settings or get_settings()
    return create_async_engine(
        cfg.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async sessionmaker."""
    return async_sessionmaker(
        get_engine(settings),
        expire_on_commit=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Context-managed AsyncSession with commit/rollback handling."""
    maker = get_sessionmaker(settings)
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
