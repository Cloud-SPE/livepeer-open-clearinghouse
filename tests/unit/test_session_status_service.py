"""Unit tests for sessions.service.get_session_status."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
    SessionNotFound,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    MockRegistryClient,
    SelectedRoute,
)
from livepeer_open_clearinghouse.settings import Settings


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


def _settings() -> Settings:
    return Settings(
        admin_bootstrap_token="x",
        session_signing_secret="x",
        database_url="sqlite+aiosqlite:///:memory:",
    )


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))


async def _seed(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email=f"{uid.hex}@example.com",
        email_verified_at=datetime.now(UTC),
        password_hash="x",
    )
    key_uid = uuid.uuid4()
    key = ApiKey(
        id=key_uid,
        user_id=user.id,
        prefix=f"loc_test_{key_uid.hex[:8]}",
        hash="h",
        label="t",
    )
    db.add_all([user, key])
    await db.flush()
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(10**12)))
    await db.flush()
    return user.id, key.id


def _route() -> SelectedRoute:
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        price_per_work_unit_wei=Decimal(1000),
        work_unit="session_second",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol="paid-session/v1",
        extra={
            "session": {
                "descriptor_schema": "test-runtime/v1",
                "metering": "runner-reported",
                "refill": "extensible",
            }
        },
    )


async def _open(db: AsyncSession):
    user_id, key_id = await _seed(db)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    open_resp = await sessions_service.open_session(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        estimated_runway_units=100,
        max_total_units=1000,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    return user_id, key_id, open_resp, daemon


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_for_open_session_has_cap_status(db_session: AsyncSession) -> None:
    user_id, _, open_resp, _ = await _open(db_session)
    status = await sessions_service.get_session_status(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        clock=_clock(),
        settings=_settings(),
    )
    assert status.session_id == open_resp.session_id
    assert status.state == SESSION_STATE_OPEN
    assert status.protocol == "paid-session/v1"
    assert status.funded_value_wei == 1_000_000
    # billed so far = initial mint EV = 100 x 1000 = 100_000
    assert status.billed_value_wei == 100_000
    assert status.refill_count == 0
    assert status.actual_units is None
    assert status.outcome is None
    assert status.closed_at is None
    assert status.cap_status is not None
    assert status.cap_status.session_pct_used == pytest.approx(0.1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_after_refills_reflects_running_billed(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open(db_session)
    # Two refills x 100_000 = 200_000 added; total billed running = 300_000
    for _ in range(2):
        await sessions_service.refill_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            api_key_id=key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )
    status = await sessions_service.get_session_status(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        clock=_clock(),
        settings=_settings(),
    )
    assert status.billed_value_wei == 300_000
    assert status.refill_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_for_closed_session_no_cap_status(db_session: AsyncSession) -> None:
    user_id, _, open_resp, _ = await _open(db_session)
    await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=400,
        outcome=None,
        settlement=None,
        clock=_clock(),
    )
    status = await sessions_service.get_session_status(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        clock=_clock(),
        settings=_settings(),
    )
    assert status.state == SESSION_STATE_CLOSED
    assert status.cap_status is None
    assert status.actual_units == 400
    # billed = 400 x 1000 = 400_000 (the persisted final value)
    assert status.billed_value_wei == 400_000
    assert status.outcome == "OVERFUNDED"
    assert status.closed_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_rejects_unknown_session(db_session: AsyncSession) -> None:
    user_id, _ = await _seed(db_session)
    with pytest.raises(SessionNotFound):
        await sessions_service.get_session_status(
            db_session,
            session_id=uuid.uuid4(),
            user_id=user_id,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_rejects_wrong_owner(db_session: AsyncSession) -> None:
    _, _, open_resp, _ = await _open(db_session)
    other_user_id, _ = await _seed(db_session)
    with pytest.raises(SessionNotFound):
        await sessions_service.get_session_status(
            db_session,
            session_id=open_resp.session_id,
            user_id=other_user_id,
            clock=_clock(),
            settings=_settings(),
        )
