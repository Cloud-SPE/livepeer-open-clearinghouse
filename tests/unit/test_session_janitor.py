"""Signed-settlement reconciliation for sessions whose SDK goes silent."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.sessions.types import CreateSessionResponse
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerSettlementQueryError,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    MockRegistryClient,
    SelectedRoute,
    SettlementKey,
)
from livepeer_open_clearinghouse.settings import Settings
from tests.fixtures.signed_settlement import delegated_key, signed_session_settlement


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fk(dbapi_conn: object, _: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 20, 12, 30, tzinfo=UTC))


def _settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:")


def _route() -> SelectedRoute:
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="livepeer:meeting",
        offering="meeting-default",
        price_per_work_unit_wei=Decimal(1000),
        work_unit="participant_minute",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol="paid-session/v1",
        settlement_keys=(SettlementKey.model_validate(delegated_key()),),
        extra={
            "session": {
                "descriptor_schema": "meeting/v1",
                "attachment": "external",
                "metering": "runner-reported",
                "refill": "extensible",
            }
        },
    )


async def _open(db: AsyncSession, clock: FrozenClock) -> CreateSessionResponse:
    user_id = uuid.uuid4()
    key_id = uuid.uuid4()
    db.add(
        User(
            id=user_id,
            email=f"{user_id.hex}@example.com",
            email_verified_at=clock.now(),
            password_hash="x",
        )
    )
    db.add(
        ApiKey(
            id=key_id,
            user_id=user_id,
            prefix=f"loc_test_{key_id.hex[:8]}",
            hash=key_id.hex,
            label="test",
        )
    )
    await db.flush()
    db.add(CreditBalance(user_id=user_id, amount_wei=Decimal(10**12)))
    await db.flush()
    response = await sessions_service.open_session(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability="livepeer:meeting",
        offering="meeting-default",
        descriptor_schema="meeting/v1",
        estimated_runway_units=100,
        max_total_units=1000,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[_route()]),
        daemon=MockPaymentDaemonClient(ev_ratio=Decimal("1.0")),
        clock=clock,
        settings=_settings(),
    )
    return response


class _SettlementClient:
    def __init__(self, records: Mapping[uuid.UUID, dict[str, Any] | None]) -> None:
        self.records = dict(records)
        self.calls: list[tuple[str, uuid.UUID]] = []

    async def get_settlement(
        self, *, broker_url: str, gateway_session_id: uuid.UUID
    ) -> dict[str, Any] | None:
        self.calls.append((broker_url, gateway_session_id))
        return self.records.get(gateway_session_id)


def _signed(
    response: CreateSessionResponse,
    *,
    state: str = "closed",
    gateway_session_id: str | None = None,
    outcome: str = "OVERFUNDED",
    actual_units: int | None = None,
    debited_units: int = 350,
) -> dict[str, Any]:
    return signed_session_settlement(
        gateway_session_id=gateway_session_id or str(response.session_id),
        work_id=response.work_id,
        actual_units=actual_units,
        debited_units=debited_units,
        billed_value_wei=350_000,
        funded_value_wei=1_000_000,
        generation_funded_value_wei=1_000_000,
        amount_wei=1000,
        per_units=1,
        work_unit="participant_minute",
        state=state,
        outcome=outcome,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_finalizes_by_gateway_id_without_sdk(db_session: AsyncSession) -> None:
    clock = _clock()
    response = await _open(db_session, clock)
    clock.advance(timedelta(seconds=61))
    client = _SettlementClient({response.session_id: _signed(response)})

    finalized = await sessions_service.reconcile_open_sessions(
        db_session, settlement_client=client, clock=clock
    )

    assert finalized == 1
    assert client.calls == [(response.broker_url, response.session_id)]
    row = await db_session.get(PaymentSession, response.session_id)
    assert row is not None
    assert row.state == sessions_service.SESSION_STATE_CLOSED
    assert row.actual_units == 350
    assert row.billed_value_wei == Decimal(350_000)
    assert row.last_polled_at == clock.now().replace(tzinfo=None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_verifies_active_record_but_leaves_session_open(
    db_session: AsyncSession,
) -> None:
    clock = _clock()
    response = await _open(db_session, clock)
    client = _SettlementClient({response.session_id: _signed(response, state="active")})

    assert (
        await sessions_service.reconcile_open_sessions(
            db_session, settlement_client=client, clock=clock
        )
        == 0
    )
    row = await db_session.get(PaymentSession, response.session_id)
    assert row is not None
    assert row.state == sessions_service.SESSION_STATE_OPEN
    assert row.last_polled_at == clock.now().replace(tzinfo=None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_and_janitor_keep_session_encumbered_when_debit_failed(
    db_session: AsyncSession,
) -> None:
    clock = _clock()
    response = await _open(db_session, clock)
    row = await db_session.get(PaymentSession, response.session_id)
    assert row is not None
    balance_before = (await db_session.get(CreditBalance, row.user_id)).amount_wei  # type: ignore[union-attr]
    failed = _signed(
        response,
        outcome="DEBIT_FAILED",
        actual_units=350,
        debited_units=0,
    )

    with pytest.raises(sessions_service.SessionSettlementVerificationFailed) as exc_info:
        await sessions_service.close_session(
            db_session,
            session_id=response.session_id,
            user_id=row.user_id,
            actual_units=350,
            outcome=None,
            settlement=failed,
            clock=clock,
        )
    assert exc_info.value.details == {"reason": "debit_failed"}

    assert (
        await sessions_service.reconcile_open_sessions(
            db_session,
            settlement_client=_SettlementClient({response.session_id: failed}),
            clock=clock,
        )
        == 0
    )
    await db_session.refresh(row)
    assert row.state == sessions_service.SESSION_STATE_OPEN
    balance_after = (await db_session.get(CreditBalance, row.user_id)).amount_wei  # type: ignore[union-attr]
    assert balance_after == balance_before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_rejects_cross_session_replay(db_session: AsyncSession) -> None:
    clock = _clock()
    response = await _open(db_session, clock)
    replay = _signed(response, gateway_session_id=str(uuid.uuid4()))

    assert (
        await sessions_service.reconcile_open_sessions(
            db_session,
            settlement_client=_SettlementClient({response.session_id: replay}),
            clock=clock,
        )
        == 0
    )
    row = await db_session.get(PaymentSession, response.session_id)
    assert row is not None
    assert row.state == sessions_service.SESSION_STATE_OPEN


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_disambiguates_sessions_that_share_work_id(
    db_session: AsyncSession,
) -> None:
    clock = _clock()
    first = await _open(db_session, clock)
    second = await _open(db_session, clock)
    second_row = await db_session.get(PaymentSession, second.session_id)
    assert second_row is not None
    second_row.work_id = first.work_id
    await db_session.flush()

    records = {
        first.session_id: _signed(first),
        second.session_id: signed_session_settlement(
            gateway_session_id=str(second.session_id),
            work_id=first.work_id,
            debited_units=350,
            billed_value_wei=350_000,
            funded_value_wei=1_000_000,
            generation_funded_value_wei=1_000_000,
            amount_wei=1000,
            per_units=1,
            work_unit="participant_minute",
        ),
    }
    client = _SettlementClient(records)

    assert (
        await sessions_service.reconcile_open_sessions(
            db_session, settlement_client=client, clock=clock
        )
        == 2
    )
    assert {gateway_id for _, gateway_id in client.calls} == {
        first.session_id,
        second.session_id,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_retries_after_query_failure(db_session: AsyncSession) -> None:
    clock = _clock()
    response = await _open(db_session, clock)

    class _UnavailableClient:
        async def get_settlement(self, **_: Any) -> None:
            raise BrokerSettlementQueryError("unavailable")

    assert (
        await sessions_service.reconcile_open_sessions(
            db_session, settlement_client=_UnavailableClient(), clock=clock
        )
        == 0
    )
    row = await db_session.get(PaymentSession, response.session_id)
    assert row is not None
    assert row.last_polled_at is None
