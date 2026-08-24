"""Integration-style tests for sessions.service.open_session.

Composes a mock payer-daemon + mock registry + in-memory aiosqlite
DB to exercise the full session-open orchestration: route discovery,
protocol validation, worst-case encumbrance, mint, and the persisted
payment_session + Payment rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
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

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_OPEN,
    InvalidSessionRequest,
    ProtocolNotSupportedForSession,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
)
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


async def _seed_user_key_and_balance(
    db: AsyncSession, *, balance_wei: int = 10**18
) -> tuple[uuid.UUID, uuid.UUID]:
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
    db.add_all([user, key])
    await db.flush()
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(balance_wei)))
    await db.flush()
    return user.id, key.id


def _route_for_protocol(protocol: str, *, refill: str = "extensible") -> SelectedRoute:
    is_session = protocol == "paid-session/v1"
    extra = (
        {
            "session": {
                "descriptor_schema": "test-runtime/v1",
                "metering": "runner-reported",
                "refill": refill,
            }
        }
        if is_session
        else {"job": {"transports": ["unary", "stream", "multipart"]}}
    )
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="openai:realtime",
        offering="openai-resale",
        price_per_work_unit_wei=Decimal(1000),
        work_unit="audio_second",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol=protocol,
        extra=extra,
    )


# ---- happy path ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_writes_session_payment_and_encumbrance(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_key_and_balance(db_session, balance_wei=10**18)
    route = _route_for_protocol("paid-session/v1", refill="bounded")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    response = await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:realtime",
        offering="openai-resale",
        estimated_runway_units=3600,  # ~1h runway
        max_total_units=7200,  # 2h ceiling
        sdk_identity="python/0.4.0/abc1234",
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    # ---- response shape
    assert response.protocol == "paid-session/v1"
    assert response.broker_url == "https://broker.example/livepeer"
    assert response.refill_endpoint == f"/v1/sessions/{response.session_id}/refill"
    assert response.close_endpoint == f"/v1/sessions/{response.session_id}/close"
    assert response.work_id  # non-empty
    assert response.payment_envelope  # non-empty base64
    # Worst case = 7200 units x 1000 wei = 7_200_000
    assert response.funded_value_wei == 7_200_000
    # Initial mint expected_value = 3600 x 1000 x 1.0 (ev_ratio) = 3_600_000
    assert response.expected_value_wei == 3_600_000

    # ---- payment_session row
    sessions = (await db_session.scalars(select(PaymentSession))).all()
    assert len(sessions) == 1
    ps = sessions[0]
    assert ps.state == SESSION_STATE_OPEN
    assert ps.protocol == "paid-session/v1"
    assert ps.broker_request_id == response.request_id
    assert ps.route_snapshot is not None
    assert ps.route_snapshot["protocol"] == "paid-session/v1"
    assert ps.route_snapshot["session"]["refill"] == "bounded"
    assert ps.route_snapshot["units_per_price"] == 1
    assert ps.route_snapshot["quote_id"] == "q-1"
    assert ps.work_id == response.work_id
    assert ps.estimated_units == 3600
    assert ps.max_total_units == 7200
    assert ps.funded_value_wei == Decimal(7_200_000)
    assert ps.sdk_identity == "python/0.4.0/abc1234"
    assert response.route_snapshot.model_dump(mode="json") == ps.route_snapshot

    # ---- Payment row linked via session_id
    payments = (await db_session.scalars(select(Payment))).all()
    assert len(payments) == 1
    p = payments[0]
    assert p.session_id == ps.id
    assert p.work_id == response.work_id
    assert p.mint_request_id == f"loc:{response.request_id}"
    # Initial ticket funded for runway (not worst case).
    assert p.funded_value_wei == Decimal(3_600_000)

    # ---- balance encumbrance
    balance = await billing_service.get_balance(db_session, user_id=user_id)
    assert balance.amount_wei == Decimal(10**18 - 7_200_000)

    # ---- credit_ledger entry tagged session_encumbrance
    ledger = (
        await db_session.scalars(select(CreditLedger).where(CreditLedger.user_id == user_id))
    ).all()
    assert any(
        e.reason == "session_encumbrance" and e.delta_wei == Decimal(-7_200_000) for e in ledger
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_payment_ev_below_initial_funding(
    db_session: AsyncSession,
) -> None:
    class UnderfundedDaemon(MockPaymentDaemonClient):
        async def create_payment(self, request):  # type: ignore[no-untyped-def]
            response = await super().create_payment(request)
            return replace(response, expected_value=Decimal(2))

    user_id, key_id = await _seed_user_key_and_balance(db_session, balance_wei=10**18)
    with pytest.raises(DaemonUnavailable, match="expected_value does not cover"):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            estimated_runway_units=3,
            max_total_units=6,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[_route_for_protocol("paid-session/v1")]),
            daemon=UnderfundedDaemon(),
            clock=_clock(),
            settings=_settings(),
        )
    assert (await db_session.scalars(select(Payment))).all() == []
    assert (await db_session.scalars(select(PaymentSession))).all() == []


# ---- error paths ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_max_below_estimated(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    route = _route_for_protocol("paid-session/v1", refill="bounded")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(InvalidSessionRequest):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            estimated_runway_units=100,
            max_total_units=50,  # max < estimated
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_no_route(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    registry = MockRegistryClient(routes=[])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(NoRouteAvailable):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="missing",
            offering="missing",
            estimated_runway_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_job_protocol(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    route = _route_for_protocol("paid-job/v1")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(ProtocolNotSupportedForSession):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            estimated_runway_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_descriptor_schema_mismatch_before_mint(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    registry = MockRegistryClient(routes=[_route_for_protocol("paid-session/v1")])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(InvalidSessionRequest, match="descriptor schema"):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            descriptor_schema="different-runtime/v1",
            estimated_runway_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )

    assert daemon._mint_replays == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_paid_job_protocol(db_session: AsyncSession) -> None:
    """paid-job/v1 routes go through POST /v1/jobs, not sessions."""
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    route = _route_for_protocol("paid-job/v1")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(ProtocolNotSupportedForSession):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            estimated_runway_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_rejects_insufficient_balance_for_worst_case(
    db_session: AsyncSession,
) -> None:
    """Worst case is 1000 wei x 100 units = 100_000 wei; balance has 50_000."""
    user_id, key_id = await _seed_user_key_and_balance(db_session, balance_wei=50_000)
    route = _route_for_protocol("paid-session/v1", refill="bounded")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient()

    with pytest.raises(InsufficientCredit):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:realtime",
            offering="openai-resale",
            estimated_runway_units=10,
            max_total_units=100,  # → worst case = 100_000 wei, balance is 50_000
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )

    # Side effects: no payment_session, no Payment, no balance debit
    assert (await db_session.scalars(select(PaymentSession))).all() == []
    assert (await db_session.scalars(select(Payment))).all() == []
    balance = await billing_service.get_balance(db_session, user_id=user_id)
    assert balance.amount_wei == Decimal(50_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_accepts_extensible_session_declaration(
    db_session: AsyncSession,
) -> None:
    """Smoke test for the extensible paid-session declaration."""
    user_id, key_id = await _seed_user_key_and_balance(db_session)
    route = _route_for_protocol("paid-session/v1")
    registry = MockRegistryClient(routes=[route])
    daemon = MockPaymentDaemonClient()

    response = await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:realtime",
        offering="openai-resale",
        estimated_runway_units=10,
        max_total_units=100,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert response.protocol == "paid-session/v1"
