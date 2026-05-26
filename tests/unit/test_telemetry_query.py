"""Tests for the customer query API (GET /v1/telemetry/events).

Service-layer tests for filtering / glob / cursor + retention-window
gate. Wire-level behavior (rate limit, ndjson streaming) is exercised
against the live dev stack rather than the FastAPI TestClient — same
pattern as the rest of the telemetry domain.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.telemetry import service
from livepeer_open_clearinghouse.domains.telemetry.config import SOURCE_SDK
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture()
async def user_and_keys(
    session: AsyncSession,
) -> tuple[User, ApiKey, ApiKey]:
    """Two API keys for one user — used to confirm cross-key scoping."""
    user = User(email="t@example.com")
    session.add(user)
    await session.flush()
    key_a = ApiKey(user_id=user.id, prefix="pymth_live_aaa", hash="h", label="a")
    key_b = ApiKey(user_id=user.id, prefix="pymth_live_bbb", hash="h", label="b")
    session.add_all([key_a, key_b])
    await session.flush()
    return user, key_a, key_b


def _ev(
    *,
    api_key: ApiKey,
    user: User,
    when: datetime,
    event_type: str = "request.mint_started",
) -> TelemetryEvent:
    return TelemetryEvent(
        api_key_id=api_key.id,
        user_id=user.id,
        event_type=event_type,
        event_schema_version=1,
        correlation_id=uuid.uuid4(),
        client_ts=when,
        received_ts=when,
        source=SOURCE_SDK,
        payload={"k": "v"},
    )


@pytest.mark.unit
class TestGlobToLike:
    def test_simple_star(self) -> None:
        assert service._glob_to_like("request.*") == "request.%"

    def test_middle_star(self) -> None:
        assert service._glob_to_like("session.refill_*") == "session.refill\\_%"

    def test_escapes_sql_wildcards(self) -> None:
        # SQL % and _ must not pass through as wildcards.
        assert service._glob_to_like("a%b_c") == "a\\%b\\_c"

    def test_no_wildcard(self) -> None:
        assert service._glob_to_like("server.mint_served") == "server.mint\\_served"


@pytest.mark.unit
class TestCursorRoundtrip:
    def test_encode_decode(self) -> None:
        ts = datetime(2026, 5, 24, 12, 30, 45, tzinfo=UTC)
        eid = uuid.UUID("11111111-2222-3333-4444-555555555555")
        cursor = service._encode_cursor(ts, eid)
        out_ts, out_id = service._decode_cursor(cursor)
        assert out_ts == ts
        assert out_id == eid

    def test_invalid_cursor_raises(self) -> None:
        with pytest.raises(service.InvalidCursor):
            service._decode_cursor("not-base64!")


@pytest.mark.unit
async def test_list_events_filters_by_api_key(
    session: AsyncSession,
    user_and_keys: tuple[User, ApiKey, ApiKey],
) -> None:
    user, key_a, key_b = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    for offset in range(3):
        session.add(_ev(api_key=key_a, user=user, when=now - timedelta(minutes=offset)))
        session.add(_ev(api_key=key_b, user=user, when=now - timedelta(minutes=offset)))
    await session.flush()

    rows, next_cursor = await service.list_events_for_api_key(
        session,
        api_key_id=key_a.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob=None,
        cursor=None,
        page_size=10,
        retention_days=30,
        clock=clock,
    )
    assert len(rows) == 3
    assert all(r.api_key_id == key_a.id for r in rows)
    assert next_cursor is None


@pytest.mark.unit
async def test_list_events_filters_by_glob(
    session: AsyncSession, user_and_keys: tuple[User, ApiKey, ApiKey]
) -> None:
    user, key, _ = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    session.add(_ev(api_key=key, user=user, when=now, event_type="request.mint_started"))
    session.add(_ev(api_key=key, user=user, when=now, event_type="request.settle_completed"))
    session.add(_ev(api_key=key, user=user, when=now, event_type="session.refill_granted"))
    session.add(_ev(api_key=key, user=user, when=now, event_type="sdk.init"))
    await session.flush()

    rows, _ = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob="request.*",
        cursor=None,
        page_size=10,
        retention_days=30,
        clock=clock,
    )
    types = sorted(r.event_type for r in rows)
    assert types == ["request.mint_started", "request.settle_completed"]


@pytest.mark.unit
async def test_list_events_paginates_with_cursor(
    session: AsyncSession, user_and_keys: tuple[User, ApiKey, ApiKey]
) -> None:
    user, key, _ = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    # 5 events, 1 minute apart
    for i in range(5):
        session.add(_ev(api_key=key, user=user, when=now - timedelta(minutes=i)))
    await session.flush()

    # Page 1: 2 rows + a cursor
    rows_a, cursor_a = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob=None,
        cursor=None,
        page_size=2,
        retention_days=30,
        clock=clock,
    )
    assert len(rows_a) == 2
    assert cursor_a is not None

    # Page 2: 2 more + another cursor
    rows_b, cursor_b = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob=None,
        cursor=cursor_a,
        page_size=2,
        retention_days=30,
        clock=clock,
    )
    assert len(rows_b) == 2
    assert cursor_b is not None

    # Page 3: last row, no cursor
    rows_c, cursor_c = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob=None,
        cursor=cursor_b,
        page_size=2,
        retention_days=30,
        clock=clock,
    )
    assert len(rows_c) == 1
    assert cursor_c is None

    # Confirm no overlap between pages.
    ids_a = {r.id for r in rows_a}
    ids_b = {r.id for r in rows_b}
    ids_c = {r.id for r in rows_c}
    assert ids_a.isdisjoint(ids_b)
    assert ids_b.isdisjoint(ids_c)
    assert ids_a.isdisjoint(ids_c)


@pytest.mark.unit
async def test_list_events_rejects_expired_window(
    session: AsyncSession, user_and_keys: tuple[User, ApiKey, ApiKey]
) -> None:
    _user, key, _ = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    with pytest.raises(service.TelemetryWindowExpired):
        await service.list_events_for_api_key(
            session,
            api_key_id=key.id,
            from_ts=now - timedelta(days=60),  # > 30d
            to_ts=now,
            event_type_glob=None,
            cursor=None,
            page_size=10,
            retention_days=30,
            clock=clock,
        )


@pytest.mark.unit
async def test_list_events_swap_bounds_returns_empty(
    session: AsyncSession, user_and_keys: tuple[User, ApiKey, ApiKey]
) -> None:
    _user, key, _ = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    rows, cursor = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now,
        to_ts=now - timedelta(hours=1),
        event_type_glob=None,
        cursor=None,
        page_size=10,
        retention_days=30,
        clock=clock,
    )
    assert rows == []
    assert cursor is None


@pytest.mark.unit
async def test_list_events_orders_newest_first(
    session: AsyncSession, user_and_keys: tuple[User, ApiKey, ApiKey]
) -> None:
    user, key, _ = user_and_keys
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)
    for i in range(3):
        session.add(_ev(api_key=key, user=user, when=now - timedelta(minutes=i)))
    await session.flush()

    rows, _ = await service.list_events_for_api_key(
        session,
        api_key_id=key.id,
        from_ts=now - timedelta(hours=1),
        to_ts=now + timedelta(hours=1),
        event_type_glob=None,
        cursor=None,
        page_size=10,
        retention_days=30,
        clock=clock,
    )
    ts_list = [r.received_ts for r in rows]
    assert ts_list == sorted(ts_list, reverse=True)
