"""Tests for telemetry service: ingest, server emit, retention purge."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
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
from livepeer_open_clearinghouse.domains.telemetry import service as telemetry_service
from livepeer_open_clearinghouse.domains.telemetry.config import (
    MAX_BATCH_SIZE,
    MAX_PAYLOAD_BYTES,
    SOURCE_SDK,
    SOURCE_SERVER,
)
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.telemetry.types import IngestEventIn
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
async def user_and_key(session: AsyncSession) -> tuple[User, ApiKey]:
    user = User(email="t@example.com")
    session.add(user)
    await session.flush()
    api_key = ApiKey(
        user_id=user.id,
        prefix="pymth_live_aaaa",
        hash="h",
        label="t",
    )
    session.add(api_key)
    await session.flush()
    return user, api_key


def _ev(
    *,
    event_type: str = "request.mint_started",
    schema: int = 1,
    payload: dict | None = None,
) -> IngestEventIn:
    return IngestEventIn(
        event_type=event_type,
        event_schema_version=schema,
        correlation_id=uuid.uuid4(),
        client_ts=datetime(2026, 5, 24, 12, 0, tzinfo=UTC),
        payload=payload or {"capability": "x", "estimated_units": 10},
    )


@pytest.mark.unit
async def test_ingest_batch_happy_path(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    clock = FrozenClock(datetime(2026, 5, 24, 12, 30, tzinfo=UTC))
    events = [_ev(), _ev(event_type="request.mint_completed"), _ev(event_type="sdk.init")]
    accepted, reasons = await telemetry_service.ingest_batch(
        session,
        api_key_id=api_key.id,
        user_id=user.id,
        events=events,
        clock=clock,
    )
    assert accepted == 3
    assert reasons == ["", "", ""]
    rows = list(
        (await session.scalars(select(TelemetryEvent).order_by(TelemetryEvent.event_type))).all()
    )
    assert len(rows) == 3
    assert all(r.source == SOURCE_SDK for r in rows)
    assert all(r.api_key_id == api_key.id for r in rows)
    assert all(r.user_id == user.id for r in rows)
    # SQLite drops tzinfo on roundtrip; compare the naive time component.
    expected = clock.now().replace(tzinfo=None)
    assert all(r.received_ts.replace(tzinfo=None) == expected for r in rows)


@pytest.mark.unit
async def test_ingest_batch_rejects_oversized_payload(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    big = "x" * (MAX_PAYLOAD_BYTES + 100)
    events = [_ev(), _ev(payload={"big": big})]
    accepted, reasons = await telemetry_service.ingest_batch(
        session,
        api_key_id=api_key.id,
        user_id=user.id,
        events=events,
        clock=FrozenClock(),
    )
    assert accepted == 1
    assert reasons[0] == ""
    assert reasons[1] == "payload_too_large"
    count = await session.scalar(select(func.count()).select_from(TelemetryEvent))
    assert count == 1


@pytest.mark.unit
async def test_ingest_batch_too_large_raises(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    events = [_ev() for _ in range(MAX_BATCH_SIZE + 1)]
    with pytest.raises(telemetry_service.BatchTooLarge):
        await telemetry_service.ingest_batch(
            session,
            api_key_id=api_key.id,
            user_id=user.id,
            events=events,
            clock=FrozenClock(),
        )


@pytest.mark.unit
async def test_record_server_event(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    clock = FrozenClock(datetime(2026, 5, 24, 12, 30, tzinfo=UTC))
    row = await telemetry_service.record_server_event(
        session,
        event_type="server.refill_denied",
        event_schema_version=1,
        payload={"which_cap": "spend_period", "remaining_wei": 0},
        api_key_id=api_key.id,
        user_id=user.id,
        correlation_id=uuid.uuid4(),
        clock=clock,
    )
    assert row.source == SOURCE_SERVER
    assert row.event_type == "server.refill_denied"
    assert row.received_ts.replace(tzinfo=None) == clock.now().replace(tzinfo=None)
    assert row.client_ts is None


@pytest.mark.unit
async def test_record_server_event_rejects_wrong_prefix(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    with pytest.raises(telemetry_service.InvalidSource):
        await telemetry_service.record_server_event(
            session,
            event_type="request.mint_started",  # missing server. prefix
            event_schema_version=1,
            payload={},
            api_key_id=api_key.id,
            user_id=user.id,
            correlation_id=None,
            clock=FrozenClock(),
        )


@pytest.mark.unit
async def test_purge_expired(session: AsyncSession, user_and_key: tuple[User, ApiKey]) -> None:
    user, api_key = user_and_key
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now)

    # 3 rows old enough to purge, 2 within the window
    for offset_days in [40, 35, 31, 5, 1]:
        ev = _ev()
        session.add(
            TelemetryEvent(
                api_key_id=api_key.id,
                user_id=user.id,
                event_type=ev.event_type,
                event_schema_version=ev.event_schema_version,
                correlation_id=ev.correlation_id,
                client_ts=ev.client_ts,
                received_ts=now - timedelta(days=offset_days),
                source=SOURCE_SDK,
                payload=ev.payload,
            )
        )
    await session.flush()

    deleted = await telemetry_service.purge_expired(session, retention_days=30, clock=clock)
    assert deleted == 3
    remaining = await session.scalar(select(func.count()).select_from(TelemetryEvent))
    assert remaining == 2


@pytest.mark.unit
async def test_purge_expired_noop_when_disabled(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    ev = _ev()
    session.add(
        TelemetryEvent(
            api_key_id=api_key.id,
            user_id=user.id,
            event_type=ev.event_type,
            event_schema_version=1,
            correlation_id=None,
            client_ts=None,
            received_ts=datetime(2020, 1, 1, tzinfo=UTC),  # ancient
            source=SOURCE_SDK,
            payload={},
        )
    )
    await session.flush()
    deleted = await telemetry_service.purge_expired(session, retention_days=0, clock=FrozenClock())
    assert deleted == 0
    remaining = await session.scalar(select(func.count()).select_from(TelemetryEvent))
    assert remaining == 1
