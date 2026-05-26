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

# Pull every domain's repo so Base.metadata.create_all knows about it.
from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.db.base import Base


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
        mode="ws-realtime@v0",
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
    assert fetched.mode == "ws-realtime@v0"
    assert fetched.state == "open"
    assert fetched.billed_value_wei is None
    assert fetched.outcome is None
    assert fetched.last_debit_seq == 0
    assert fetched.sdk_identity == "python/0.4.0/abc1234"


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
        mode="session-control-plus-media@v0",
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
        mode="ws-realtime@v0",
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
