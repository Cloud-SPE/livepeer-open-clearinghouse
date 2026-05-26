"""Tests for the server.* event emission helpers + their integration
into the mint / refill / janitor flows.

The helpers themselves (emit_mint_served, etc.) are thin wrappers
around record_server_event; we exercise the integration paths to
catch wire-up regressions where a service stops calling the helper.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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
from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.admin.repo import Operator
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.telemetry import server_events
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import InsufficientCredit
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
    def _enable_fk(dbapi_conn: object, _: object) -> None:
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
    return FrozenClock(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))


async def _seed_user(db: AsyncSession, *, balance_wei: int) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(email=f"u-{uuid.uuid4().hex[:8]}@example.com")
    db.add(user)
    await db.flush()
    api_key = ApiKey(
        user_id=user.id,
        prefix=f"pymth_live_{uuid.uuid4().hex[:8]}",
        hash="h",
        label="t",
    )
    db.add(api_key)
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(balance_wei)))
    await db.flush()
    return user.id, api_key.id


@pytest.mark.unit
async def test_mint_refused_fires_on_insufficient_credit(
    db_session: AsyncSession,
) -> None:
    user_id, api_key_id = await _seed_user(db_session, balance_wei=100)
    registry = MockRegistryClient(
        routes=[
            SelectedRoute(
                eth_address="0x" + "00" * 20,
                worker_url="http://broker.local",
                capability="cap.x",
                offering="off.y",
                price_per_work_unit_wei=10,
                units_per_price=1,
                work_unit="tok",
                quote_id="q",
                quote_version=1,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x00" * 32,
                extra={"interaction_mode": "session-control-plus-media@v0"},
            )
        ]
    )
    daemon = MockPaymentDaemonClient()

    with pytest.raises(InsufficientCredit):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=api_key_id,
            capability="cap.x",
            offering="off.y",
            estimated_runway_units=10,
            max_total_units=100,  # 100 * 10 = 1000 wei > 100 balance
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )

    rows = list(
        (
            await db_session.scalars(
                select(TelemetryEvent).where(TelemetryEvent.event_type == "server.mint_refused")
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].payload["which_cap"] == "user_balance"
    assert rows[0].payload["remaining_wei"] == 100
    assert rows[0].source == "server"


@pytest.mark.unit
async def test_mint_served_fires_on_open_session_success(
    db_session: AsyncSession,
) -> None:
    user_id, api_key_id = await _seed_user(db_session, balance_wei=10_000)
    registry = MockRegistryClient(
        routes=[
            SelectedRoute(
                eth_address="0x" + "00" * 20,
                worker_url="http://broker.local",
                capability="cap.x",
                offering="off.y",
                price_per_work_unit_wei=10,
                units_per_price=1,
                work_unit="tok",
                quote_id="q",
                quote_version=1,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x00" * 32,
                extra={"interaction_mode": "session-control-plus-media@v0"},
            )
        ]
    )
    daemon = MockPaymentDaemonClient()

    await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=api_key_id,
        capability="cap.x",
        offering="off.y",
        estimated_runway_units=10,
        max_total_units=100,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    rows = list(
        (
            await db_session.scalars(
                select(TelemetryEvent).where(TelemetryEvent.event_type == "server.mint_served")
            )
        ).all()
    )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["capability"] == "cap.x"
    assert payload["offering"] == "off.y"
    assert payload["mode"] == "session-control-plus-media@v0"
    assert payload["estimated_units"] == 10
    assert payload["funded_value_wei"] == 1000  # 100 * 10
    assert isinstance(payload["mint_latency_ms"], int)
    assert payload["mint_latency_ms"] >= 0


@pytest.mark.unit
async def test_sdk_sha_mismatch_fires_when_identity_unknown(
    db_session: AsyncSession,
) -> None:
    user_id, api_key_id = await _seed_user(db_session, balance_wei=10_000)
    registry = MockRegistryClient(
        routes=[
            SelectedRoute(
                eth_address="0x" + "00" * 20,
                worker_url="http://broker.local",
                capability="cap.x",
                offering="off.y",
                price_per_work_unit_wei=10,
                units_per_price=1,
                work_unit="tok",
                quote_id="q",
                quote_version=1,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x00" * 32,
                extra={"interaction_mode": "session-control-plus-media@v0"},
            )
        ]
    )
    daemon = MockPaymentDaemonClient()

    await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=api_key_id,
        capability="cap.x",
        offering="off.y",
        estimated_runway_units=10,
        max_total_units=100,
        sdk_identity="python/0.99.0/notinmanifest",
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    rows = list(
        (
            await db_session.scalars(
                select(TelemetryEvent).where(TelemetryEvent.event_type == "server.sdk_sha_mismatch")
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].payload["lang"] == "python"
    assert rows[0].payload["semver"] == "0.99.0"
    assert rows[0].payload["reported_sha"] == "notinmanifest"
    assert rows[0].payload["observed_status"] == "unknown"


@pytest.mark.unit
async def test_sdk_sha_mismatch_silent_when_identity_approved(
    db_session: AsyncSession,
) -> None:
    user_id, api_key_id = await _seed_user(db_session, balance_wei=10_000)

    # Seed an operator + approved entry in sdk_approval
    operator = Operator(
        email="op@example.com",
        name="Op",
        token_hash="h",
        role="owner",
    )
    db_session.add(operator)
    await db_session.flush()
    await admin_service.create_sdk_approval(
        db_session,
        acting_operator=operator,
        lang="python",
        version="0.2.0",
        git_sha7="abc1234",
        status="approved",
        notes=None,
    )

    registry = MockRegistryClient(
        routes=[
            SelectedRoute(
                eth_address="0x" + "00" * 20,
                worker_url="http://broker.local",
                capability="cap.x",
                offering="off.y",
                price_per_work_unit_wei=10,
                units_per_price=1,
                work_unit="tok",
                quote_id="q",
                quote_version=1,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x00" * 32,
                extra={"interaction_mode": "session-control-plus-media@v0"},
            )
        ]
    )
    daemon = MockPaymentDaemonClient()

    await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=api_key_id,
        capability="cap.x",
        offering="off.y",
        estimated_runway_units=10,
        max_total_units=100,
        sdk_identity="python/0.2.0/abc1234",
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    rows = list(
        (
            await db_session.scalars(
                select(TelemetryEvent).where(TelemetryEvent.event_type == "server.sdk_sha_mismatch")
            )
        ).all()
    )
    assert rows == []


@pytest.mark.unit
async def test_emit_helpers_swallow_failure(db_session: AsyncSession) -> None:
    """Telemetry must never raise into the data plane. Pass a session
    whose flush is broken to confirm the wrapper logs + returns."""
    user_id, api_key_id = await _seed_user(db_session, balance_wei=1)

    class _BrokenSession:
        def add(self, *args, **kwargs):
            raise RuntimeError("simulated db failure")

        def add_all(self, *args, **kwargs):
            raise RuntimeError("simulated db failure")

        async def flush(self):
            raise RuntimeError("simulated db failure")

    # Should NOT raise.
    await server_events.emit_mint_served(
        _BrokenSession(),  # type: ignore[arg-type]
        api_key_id=api_key_id,
        user_id=user_id,
        capability="x",
        offering="y",
        mode="m",
        estimated_units=1,
        funded_value_wei=1,
        mint_latency_ms=1,
        correlation_id=uuid.uuid4(),
        clock=_clock(),
    )
