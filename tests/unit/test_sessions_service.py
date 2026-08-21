"""Unit tests for domains/sessions/service.py.

Covers create_session / get_session(_by_work_id) / transition_state
/ record_settlement / mark_polled against an in-memory aiosqlite DB.
SQLite FK enforcement is enabled so cascade behavior matches
production Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Pull every domain's repo so Base.metadata.create_all knows about it.
from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_DRAINING,
    SESSION_STATE_OPEN,
    InvalidSessionState,
    InvalidSessionTransition,
    SessionNotFound,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fk(dbapi_conn: object, _: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(s: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(
        id=uuid.uuid4(),
        email="a@example.com",
        email_verified_at=datetime.now(UTC),
        password_hash="x",
    )
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        prefix="loc_test",
        hash="h",
        label="t",
    )
    s.add_all([user, key])
    await s.flush()
    return user.id, key.id


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))


# ---- create_session ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_session_writes_row_in_open_state(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="abc",
        capability="cap",
        offering="off",
        protocol="paid-session/v1",
        estimated_units=10,
        max_total_units=100,
        funded_value_wei=Decimal(1000),
        clock=_clock(),
        sdk_identity="python/0.4.0/abc1234",
    )
    assert row.state == SESSION_STATE_OPEN
    assert row.work_id == "abc"
    assert row.protocol == "paid-session/v1"
    assert row.sdk_identity == "python/0.4.0/abc1234"
    assert row.opened_at is not None
    assert row.closed_at is None
    assert row.refill_seq == 0


# ---- get_session(_by_work_id) ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_session_returns_none_for_missing(db_session: AsyncSession) -> None:
    result = await sessions_service.get_session(db_session, uuid.uuid4())
    assert result is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_session_by_work_id_returns_most_recent(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    clock = _clock()
    # Older session with same work_id.
    older = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="shared",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=clock,
    )
    # Advance the clock and create a newer one with the same work_id.
    clock.advance(timedelta(seconds=10))
    newer = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="shared",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=2,
        max_total_units=2,
        funded_value_wei=Decimal(2),
        clock=clock,
    )
    found = await sessions_service.get_session_by_work_id(db_session, "shared")
    assert found is not None
    assert found.id == newer.id
    # Make sure both rows exist (we didn't accidentally overwrite).
    all_rows = (await db_session.scalars(select(PaymentSession))).all()
    assert {r.id for r in all_rows} == {older.id, newer.id}


# ---- transition_state ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_open_to_draining_to_closed(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    clock = _clock()
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=clock,
    )

    clock.advance(timedelta(seconds=30))
    drained = await sessions_service.transition_state(
        db_session,
        row.id,
        from_state=SESSION_STATE_OPEN,
        to_state=SESSION_STATE_DRAINING,
        clock=clock,
    )
    assert drained.state == SESSION_STATE_DRAINING
    assert drained.closed_at is None

    clock.advance(timedelta(seconds=10))
    closed = await sessions_service.transition_state(
        db_session,
        row.id,
        from_state=SESSION_STATE_DRAINING,
        to_state=SESSION_STATE_CLOSED,
        clock=clock,
    )
    assert closed.state == SESSION_STATE_CLOSED
    assert closed.closed_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_open_to_closed_is_allowed_fast_close(
    db_session: AsyncSession,
) -> None:
    """Atomic jobs finish synchronously without draining."""
    user_id, key_id = await _seed(db_session)
    clock = _clock()
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-job/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=clock,
    )
    closed = await sessions_service.transition_state(
        db_session,
        row.id,
        from_state=SESSION_STATE_OPEN,
        to_state=SESSION_STATE_CLOSED,
        clock=clock,
    )
    assert closed.state == SESSION_STATE_CLOSED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_rejects_invalid_state_name(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=_clock(),
    )
    with pytest.raises(InvalidSessionState):
        await sessions_service.transition_state(
            db_session,
            row.id,
            from_state=SESSION_STATE_OPEN,
            to_state="bogus",
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_rejects_disallowed_move(
    db_session: AsyncSession,
) -> None:
    """closed → anything is forbidden; closed is terminal."""
    user_id, key_id = await _seed(db_session)
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=_clock(),
    )
    await sessions_service.transition_state(
        db_session,
        row.id,
        from_state=SESSION_STATE_OPEN,
        to_state=SESSION_STATE_CLOSED,
        clock=_clock(),
    )
    with pytest.raises(InvalidSessionTransition):
        await sessions_service.transition_state(
            db_session,
            row.id,
            from_state=SESSION_STATE_CLOSED,
            to_state=SESSION_STATE_OPEN,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_rejects_when_actual_state_diverges(
    db_session: AsyncSession,
) -> None:
    """Optimistic-concurrency guard: caller's `from_state` must match
    the row's current state."""
    user_id, key_id = await _seed(db_session)
    row = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=_clock(),
    )
    # Row is `open`, but caller claims it's `draining`.
    with pytest.raises(InvalidSessionTransition):
        await sessions_service.transition_state(
            db_session,
            row.id,
            from_state=SESSION_STATE_DRAINING,
            to_state=SESSION_STATE_CLOSED,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transition_raises_session_not_found_when_missing(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(SessionNotFound):
        await sessions_service.transition_state(
            db_session,
            uuid.uuid4(),
            from_state=SESSION_STATE_OPEN,
            to_state=SESSION_STATE_CLOSED,
            clock=_clock(),
        )


# ---- record_settlement ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_settlement_appends_event(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    sess = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1000),
        clock=_clock(),
    )

    event = await sessions_service.record_settlement(
        db_session,
        sess.id,
        event_type="close",
        clock=_clock(),
        actual_units=42,
        billed_value_wei=Decimal(420),
        outcome="OVERFUNDED",
        raw_record={"breakdown": {"input_tokens": 10, "output_tokens": 32}},
    )
    assert event.event_type == "close"
    assert event.actual_units == 42
    assert event.outcome == "OVERFUNDED"

    all_events = (
        await db_session.scalars(
            select(PaymentSettlement).where(PaymentSettlement.session_id == sess.id)
        )
    ).all()
    assert len(all_events) == 1


# ---- mark_polled ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_polled_updates_last_polled_at(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    clock = _clock()
    sess = await sessions_service.create_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        clock=clock,
    )
    assert sess.last_polled_at is None

    clock.advance(timedelta(seconds=60))
    await sessions_service.mark_polled(db_session, sess.id, clock=clock)
    refreshed = await sessions_service.get_session(db_session, sess.id)
    assert refreshed is not None
    assert refreshed.last_polled_at == clock.now()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_polled_is_noop_for_missing_session(
    db_session: AsyncSession,
) -> None:
    # Just verify it doesn't raise.
    await sessions_service.mark_polled(db_session, uuid.uuid4(), clock=_clock())
