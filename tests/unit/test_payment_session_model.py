"""Schema-level tests for the sessions domain.

Covers the PaymentSession + PaymentSettlement ORM models and the new
nullable session_id FK on Payment. Verifies columns, constraints, FK
shape, and basic round-trip persistence against an in-memory SQLite
DB (sufficient for schema-shape verification; JSONB-specific tests
live in real integration suites).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
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
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
    SpendAuthorizationGrant,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon import SelectedRoute


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # SQLite doesn't enforce FK constraints (including ON DELETE
    # CASCADE) unless `PRAGMA foreign_keys = ON` is set per
    # connection. Postgres always enforces; we mirror that here so
    # the cascade test exercises the same behavior as production.
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


async def _seed_user_and_key(s: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(
        id=uuid.uuid4(),
        email="alice@example.com",
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


def _wholesale_route() -> SelectedRoute:
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="openai:realtime",
        offering="retail-plan",
        price_per_work_unit_wei=Decimal(10),
        work_unit="audio_second",
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
            },
        },
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_session_round_trip(session: AsyncSession) -> None:
    user_id, key_id = await _seed_user_and_key(session)
    ps = PaymentSession(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        work_id="abc123",
        capability="openai:realtime",
        offering="openai-resale",
        protocol="paid-session/v1",
        broker_request_id="request-1",
        route_snapshot={"protocol": "paid-session/v1", "axes": {"refill": "bounded"}},
        state="open",
        estimated_units=3600,
        max_total_units=7200,
        funded_value_wei=Decimal("1000000000000000000"),
        opened_at=datetime.now(UTC),
        sdk_identity="python/0.4.0/abc1234",
    )
    session.add(ps)
    await session.flush()

    fetched = (await session.scalars(select(PaymentSession))).one()
    assert fetched.work_id == "abc123"
    assert fetched.protocol == "paid-session/v1"
    assert fetched.broker_request_id == "request-1"
    assert fetched.route_snapshot == {
        "protocol": "paid-session/v1",
        "axes": {"refill": "bounded"},
    }
    assert fetched.state == "open"
    assert fetched.billed_value_wei is None
    assert fetched.outcome is None
    assert fetched.refill_seq == 0
    assert fetched.rotation_generation == 0
    assert fetched.sdk_identity == "python/0.4.0/abc1234"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_customer_hold_can_reference_engagement_without_payment(
    session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_and_key(session)
    engagement = PaymentSession(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        work_id="",
        capability="openai:chat-completions",
        offering="retail-plan",
        protocol="paid-job/v1",
        accounting_mode="wholesale_account",
        customer_pricing={
            "schema_version": "customer-pricing/v1",
            "plan_id": "retail-a",
            "kind": "unit_price",
            "denomination": "wei",
            "work_unit": "tokens",
            "price_per_unit_wei": "10",
            "units_per_price": 1,
            "fee_basis_points": None,
        },
        customer_max_debit_wei=Decimal(100),
        authorization_id="auth-1",
        state="open",
        estimated_units=5,
        max_total_units=10,
        funded_value_wei=Decimal(100),
        opened_at=datetime.now(UTC),
    )
    session.add_all([engagement, CreditBalance(user_id=user_id, amount_wei=1000, version=0)])
    await session.flush()

    await billing_service.encumber_customer_engagement(
        session,
        user_id=user_id,
        engagement_id=engagement.id,
        amount_wei=Decimal(100),
        clock=FrozenClock(datetime.now(UTC)),
        period_seconds=86_400,
        cap_wei=0,
    )
    await session.flush()

    ledger = (
        await session.scalars(
            select(CreditLedger).where(CreditLedger.related_engagement_id == engagement.id)
        )
    ).one()
    assert ledger.related_payment_id is None
    assert ledger.delta_wei == Decimal(-100)

    balance = await billing_service.encumber_customer_engagement(
        session,
        user_id=user_id,
        engagement_id=engagement.id,
        amount_wei=Decimal(100),
        clock=FrozenClock(datetime.now(UTC)),
        period_seconds=86_400,
        cap_wei=0,
    )
    await session.flush()
    assert balance.amount_wei == Decimal(900)
    assert (
        len(
            (
                await session.scalars(
                    select(CreditLedger).where(CreditLedger.related_engagement_id == engagement.id)
                )
            ).all()
        )
        == 1
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorization_revisions_preserve_original_grant(
    session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_and_key(session)
    route = _wholesale_route()
    engagement = PaymentSession(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        work_id="",
        capability=route.capability,
        offering=route.offering,
        protocol=route.protocol,
        accounting_mode="wholesale_account",
        route_snapshot=route.snapshot(),
        broker_request_id="request-1",
        state="open",
        estimated_units=5,
        max_total_units=10,
        funded_value_wei=Decimal(100),
        customer_max_debit_wei=Decimal(100),
        opened_at=datetime.now(UTC),
    )
    session.add(engagement)
    await session.flush()
    daemon = MockPaymentDaemonClient()
    now = datetime.now(UTC)
    caller_key = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")

    first_request, first_response = await payments_service.issue_route_locked_authorization(
        daemon=daemon,
        route=route,
        authorization_id="auth-0",
        request_id="request-1",
        session_id=str(engagement.id),
        request_digest=b"\x22" * 32,
        caller_public_key=caller_key,
        max_debit_wei=Decimal(100),
        max_total_units=10,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        chain_id=42161,
    )
    first = await sessions_service.record_spend_authorization_grant(
        session,
        engagement_id=engagement.id,
        user_id=user_id,
        route=route,
        request=first_request,
        response=first_response,
    )
    replayed = await sessions_service.record_spend_authorization_grant(
        session,
        engagement_id=engagement.id,
        user_id=user_id,
        route=route,
        request=first_request,
        response=first_response,
    )
    assert replayed.id == first.id
    with pytest.raises(
        sessions_service.InvalidSessionRequest,
        match="authorization revision replay changed scope",
    ):
        await sessions_service.record_spend_authorization_grant(
            session,
            engagement_id=engagement.id,
            user_id=user_id,
            route=route,
            request=replace(first_request, max_debit_wei=Decimal(101)),
            response=first_response,
        )
    second_request, second_response = await payments_service.issue_route_locked_authorization(
        daemon=daemon,
        route=route,
        authorization_id="auth-1",
        request_id="request-1",
        session_id=str(engagement.id),
        request_digest=b"\x33" * 32,
        caller_public_key=caller_key,
        max_debit_wei=Decimal(200),
        max_total_units=20,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        chain_id=42161,
        revision=1,
        predecessor_authorization_id=first.authorization_id,
    )
    second = await sessions_service.record_spend_authorization_grant(
        session,
        engagement_id=engagement.id,
        user_id=user_id,
        route=route,
        request=second_request,
        response=second_response,
    )

    grants = list(
        (
            await session.scalars(
                select(SpendAuthorizationGrant).order_by(SpendAuthorizationGrant.revision)
            )
        ).all()
    )
    assert [grant.authorization_id for grant in grants] == ["auth-0", "auth-1"]
    assert first.retired_at is None
    assert first.state == "issued"
    assert first.authorization_bytes == first_response.authorization_bytes
    assert first.chain_id == 42161
    assert first.denomination == "wei"
    assert second.predecessor_authorization_id == "auth-0"
    assert engagement.authorization_id == "auth-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorization_issuance_requires_caller_proof_without_feature_negotiation() -> None:
    route = _wholesale_route()
    now = datetime.now(UTC)
    values = {
        "daemon": MockPaymentDaemonClient(),
        "authorization_id": "auth-0",
        "request_id": "request-1",
        "session_id": str(uuid.uuid4()),
        "request_digest": b"\x22" * 32,
        "max_debit_wei": Decimal(100),
        "max_total_units": 10,
        "not_before": now,
        "expires_at": now + timedelta(minutes=5),
        "chain_id": 42161,
    }
    with pytest.raises(ValueError, match="caller proof"):
        await payments_service.issue_route_locked_authorization(
            route=route,
            caller_public_key=b"",
            **values,  # type: ignore[arg-type]
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_settlement_cascades_on_session_delete(
    session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_user_and_key(session)
    ps = PaymentSession(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        state="closed",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        opened_at=datetime.now(UTC),
        closed_at=datetime.now(UTC),
    )
    session.add(ps)
    await session.flush()

    settlement = PaymentSettlement(
        id=uuid.uuid4(),
        session_id=ps.id,
        recorded_at=datetime.now(UTC),
        event_type="close",
        actual_units=42,
        billed_value_wei=Decimal(420),
        outcome="EXACT",
        raw_record={"foo": "bar"},
    )
    session.add(settlement)
    await session.flush()

    # Delete the session; cascade should remove the settlement.
    await session.delete(ps)
    await session.flush()

    remaining = (await session.scalars(select(PaymentSettlement))).all()
    assert remaining == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_session_id_fk_round_trip(session: AsyncSession) -> None:
    user_id, key_id = await _seed_user_and_key(session)
    ps = PaymentSession(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        work_id="w",
        capability="c",
        offering="o",
        protocol="paid-session/v1",
        state="open",
        estimated_units=1,
        max_total_units=1,
        funded_value_wei=Decimal(1),
        opened_at=datetime.now(UTC),
    )
    session.add(ps)
    await session.flush()

    payment = Payment(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        session_id=ps.id,
        work_id="w",
        recipient_eth_address="0xabc",
        capability="c",
        offering="o",
        work_units_requested=10,
        price_per_work_unit_wei=Decimal(1),
        funded_value_wei=Decimal(10),
        expected_value_wei=Decimal(10),
        reserved_wei=Decimal(10),
        status="reserved",
    )
    session.add(payment)
    await session.flush()

    fetched = (await session.scalars(select(Payment))).one()
    assert fetched.session_id == ps.id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_payment_session_id_can_be_null(session: AsyncSession) -> None:
    """Legacy single-shot mints carry NULL session_id."""
    user_id, key_id = await _seed_user_and_key(session)
    payment = Payment(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=key_id,
        session_id=None,
        work_id="w",
        recipient_eth_address="0xabc",
        capability="c",
        offering="o",
        work_units_requested=1,
        price_per_work_unit_wei=Decimal(1),
        funded_value_wei=Decimal(1),
        expected_value_wei=Decimal(1),
        reserved_wei=Decimal(1),
        status="reserved",
    )
    session.add(payment)
    await session.flush()

    fetched = (await session.scalars(select(Payment))).one()
    assert fetched.session_id is None
