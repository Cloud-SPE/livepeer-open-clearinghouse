"""Verify that emit_mint_refused / emit_refill_denied persist their
telemetry_event rows through an independent session so the row
survives the outer request rollback that follows the refusal raise.

The production code path opens a fresh session against the global
engine via ``session_scope()``. To test the rollback-survival
guarantee deterministically, we point both the outer session and the
helper-injected ``independent_session_factory`` at the same on-disk
sqlite database — two connections, two independent transactions, so
rolling back the outer one cannot affect the row the helper committed.
The cap_reached notification cascade is monkeypatched to a no-op so
the test stays hermetic (otherwise it would try to send a real email
through the configured provider).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.telemetry import server_events as events
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[User, ApiKey, async_sessionmaker[AsyncSession]]]:
    # Silence the cap_reached notification fan-out — the test focuses
    # on the telemetry row, not the downstream notification cascade.
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(events, "_maybe_notify_cap_reached", _noop)

    engine_url = f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}"
    engine = create_async_engine(engine_url, echo=False)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn: object, _: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as setup:
        u = User(email="rollback@example.com")
        setup.add(u)
        await setup.flush()
        key = ApiKey(
            user_id=u.id,
            prefix=f"pymth_live_{uuid.uuid4().hex[:8]}",
            hash="x" * 64,
            label="rollback-test",
        )
        setup.add(key)
        await setup.commit()
        user_id = u.id
        api_key_id = key.id

    async with maker() as fresh:
        u2 = await fresh.get(User, user_id)
        k2 = await fresh.get(ApiKey, api_key_id)
        assert u2 is not None
        assert k2 is not None

    yield u2, k2, maker
    await engine.dispose()


@asynccontextmanager
async def _factory_for(maker: async_sessionmaker[AsyncSession]):
    """Independent-session factory that opens against the same engine
    and commits on exit — mirrors the production session_scope() but
    without depending on the global engine the helper imports lazily.
    """
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest.mark.unit
async def test_emit_mint_refused_survives_outer_rollback(
    fixtures: tuple[User, ApiKey, async_sessionmaker[AsyncSession]],
) -> None:
    user, api_key, maker = fixtures

    async with maker() as outer:
        # Establish an outer transaction we will later roll back. The
        # outer doesn't actually write anything (sqlite single-writer
        # would deadlock with the independent session), but rolling it
        # back is what production does on the refusal raise path —
        # the test demonstrates that the inner commit survives.
        await events.emit_mint_refused(
            outer,
            api_key_id=api_key.id,
            user_id=user.id,
            capability="cap.example",
            offering="off.example",
            which_cap="session_max_total_units",
            remaining_wei=0,
            clock=FrozenClock(),
            independent_session_factory=lambda: _factory_for(maker),
        )
        await outer.rollback()

    async with maker() as verify:
        rows = list(
            (
                await verify.scalars(
                    select(TelemetryEvent).where(
                        TelemetryEvent.event_type == "server.mint_refused"
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].api_key_id == api_key.id
        assert rows[0].payload["which_cap"] == "session_max_total_units"
        assert rows[0].payload["capability"] == "cap.example"


@pytest.mark.unit
async def test_emit_refill_denied_survives_outer_rollback(
    fixtures: tuple[User, ApiKey, async_sessionmaker[AsyncSession]],
) -> None:
    user, api_key, maker = fixtures
    session_id = uuid.uuid4()

    async with maker() as outer:
        await events.emit_refill_denied(
            outer,
            api_key_id=api_key.id,
            user_id=user.id,
            session_id=session_id,
            refill_seq=3,
            which_cap="user_balance",
            remaining_wei=0,
            clock=FrozenClock(),
            independent_session_factory=lambda: _factory_for(maker),
        )
        await outer.rollback()

    async with maker() as verify:
        row = (
            await verify.scalars(
                select(TelemetryEvent).where(
                    TelemetryEvent.event_type == "server.refill_denied"
                )
            )
        ).one()
        assert row.api_key_id == api_key.id
        assert row.correlation_id == session_id
        assert row.payload["refill_seq"] == 3
        assert row.payload["which_cap"] == "user_balance"


@pytest.mark.unit
async def test_safe_emit_independent_swallows_factory_failure(
    fixtures: tuple[User, ApiKey, async_sessionmaker[AsyncSession]],
) -> None:
    """A misbehaving factory must NOT propagate into the caller —
    telemetry is best-effort and never breaks the data plane."""
    user, api_key, _ = fixtures

    @asynccontextmanager
    async def _boom():
        raise RuntimeError("factory blew up")
        yield  # pragma: no cover

    # Should not raise.
    await events.emit_mint_refused(
        None,  # type: ignore[arg-type]
        api_key_id=api_key.id,
        user_id=user.id,
        capability="cap.example",
        offering="off.example",
        which_cap="user_balance",
        remaining_wei=0,
        clock=FrozenClock(),
        independent_session_factory=_boom,
    )
