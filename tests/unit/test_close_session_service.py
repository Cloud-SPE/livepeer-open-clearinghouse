"""Integration-style tests for sessions.service.close_session.

Opens a session via open_session, optionally refills, then exercises
close across the three accounting outcomes (OVERFUNDED / EXACT /
UNDERFUNDED) plus error paths (already-closed, ownership, missing).
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
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SessionNotFound,
    SessionNotOpen,
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
        admin_bootstrap_token="x",  # noqa: S106
        session_signing_secret="x",  # noqa: S106
        database_url="sqlite+aiosqlite:///:memory:",
    )


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))


async def _seed(db: AsyncSession, *, balance_wei: int = 10**12) -> tuple[uuid.UUID, uuid.UUID]:
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email=f"{uid.hex}@example.com",
        email_verified_at=datetime.now(UTC),
        password_hash="x",  # noqa: S106
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
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(balance_wei)))
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
        extra={"interaction_mode": "session-control-plus-media@v0"},
    )


async def _open_session(db: AsyncSession, *, max_total: int = 1000):
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
        max_total_units=max_total,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    return user_id, key_id, open_resp, daemon


# ---- happy paths (each outcome) ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_overfunded_refunds_unused_encumbrance(
    db_session: AsyncSession,
) -> None:
    """funded=1_000_000; SDK reports actual=400 units → billed=400_000;
    refund=600_000. payment_session updated; balance refunded."""
    user_id, _, open_resp, _ = await _open_session(db_session, max_total=1000)
    balance_before_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    close_resp = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=400,
        outcome=None,  # let LOC infer
        settlement={"breakdown": {"input": 100, "output": 300}},
        clock=_clock(),
    )

    assert close_resp.actual_units == 400
    assert close_resp.billed_value_wei == 400_000
    assert close_resp.refund_wei == 600_000
    assert close_resp.outcome == "OVERFUNDED"

    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_CLOSED
    assert ps.actual_units == 400
    assert ps.billed_value_wei == Decimal(400_000)
    assert ps.outcome == "OVERFUNDED"
    assert ps.closed_at is not None

    balance_after_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    # Refund credits 600_000 back
    assert balance_after_wei - balance_before_wei == Decimal(600_000)

    # close settlement event written with the raw record
    events = (
        await db_session.scalars(
            select(PaymentSettlement).where(
                PaymentSettlement.session_id == open_resp.session_id,
                PaymentSettlement.event_type == "close",
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].outcome == "OVERFUNDED"
    assert events[0].raw_record == {"breakdown": {"input": 100, "output": 300}}

    # release_session_encumbrance ledger entry
    ledger = (
        await db_session.scalars(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id,
                CreditLedger.reason == "session_release",
            )
        )
    ).all()
    assert len(ledger) == 1
    assert ledger[0].delta_wei == Decimal(600_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_exact_no_refund(db_session: AsyncSession) -> None:
    """billed exactly matches funded → outcome=EXACT, refund_wei=0."""
    user_id, _, open_resp, _ = await _open_session(db_session, max_total=1000)
    balance_before_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    close_resp = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=1000,  # x 1000 wei = 1_000_000 = funded
        outcome=None,
        settlement=None,
        clock=_clock(),
    )
    assert close_resp.billed_value_wei == 1_000_000
    assert close_resp.refund_wei == 0
    assert close_resp.outcome == "EXACT"

    balance_after_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_after_wei == balance_before_wei


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_underfunded_no_balance_credit(db_session: AsyncSession) -> None:
    """billed exceeds funded — operator absorbs; no refund, no debit."""
    user_id, _, open_resp, _ = await _open_session(db_session, max_total=1000)
    balance_before_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    close_resp = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=1500,  # x 1000 wei = 1_500_000 > funded 1_000_000
        outcome=None,
        settlement=None,
        clock=_clock(),
    )
    assert close_resp.billed_value_wei == 1_500_000
    assert close_resp.refund_wei == 0  # clamped at 0
    assert close_resp.outcome == "UNDERFUNDED"

    balance_after_wei = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_after_wei == balance_before_wei

    # No session_release ledger entry
    ledger = (
        await db_session.scalars(
            select(CreditLedger).where(
                CreditLedger.user_id == user_id,
                CreditLedger.reason == "session_release",
            )
        )
    ).all()
    assert ledger == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_accepts_sdk_supplied_outcome(db_session: AsyncSession) -> None:
    """SDK-supplied outcome (e.g. STOPPED_AT_BUDGET) overrides inference."""
    user_id, _, open_resp, _ = await _open_session(db_session, max_total=1000)
    close_resp = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=400,
        outcome="STOPPED_AT_BUDGET",
        settlement=None,
        clock=_clock(),
    )
    assert close_resp.outcome == "STOPPED_AT_BUDGET"


# ---- error paths ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_rejects_unknown_session(db_session: AsyncSession) -> None:
    user_id, _ = await _seed(db_session)
    with pytest.raises(SessionNotFound):
        await sessions_service.close_session(
            db_session,
            session_id=uuid.uuid4(),
            user_id=user_id,
            actual_units=0,
            outcome=None,
            settlement=None,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_rejects_wrong_owner(db_session: AsyncSession) -> None:
    _, _, open_resp, _ = await _open_session(db_session)
    other_user_id, _ = await _seed(db_session)
    with pytest.raises(SessionNotFound):
        await sessions_service.close_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=other_user_id,
            actual_units=0,
            outcome=None,
            settlement=None,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_is_not_idempotent_second_call_409(
    db_session: AsyncSession,
) -> None:
    """Per the runtime docstring: a second close on an already-closed
    session raises SessionNotOpen (409), not a no-op."""
    user_id, _, open_resp, _ = await _open_session(db_session)
    await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=100,
        outcome=None,
        settlement=None,
        clock=_clock(),
    )
    with pytest.raises(SessionNotOpen):
        await sessions_service.close_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            actual_units=100,
            outcome=None,
            settlement=None,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_after_refills_billed_against_session_funded(
    db_session: AsyncSession,
) -> None:
    """Refill several times to mint multiple Payment rows. Close with
    actual_units that exceeds the initial mint's runway but less than
    total funded — verifies billing math uses funded (not initial mint
    EV) and refunds correctly."""
    user_id, key_id, open_resp, daemon = await _open_session(db_session, max_total=1000)
    # Refill 3 times. Each adds 100 units x 1000 wei = 100_000 EV worth
    # of tickets (advisory; the actual debit at close uses
    # actual_units x price).
    for _ in range(3):
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

    close_resp = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=350,  # x 1000 = 350_000 billed
        outcome=None,
        settlement=None,
        clock=_clock(),
    )
    assert close_resp.billed_value_wei == 350_000
    # funded (worst case) was 1_000_000; refund 650_000
    assert close_resp.refund_wei == 650_000
    assert close_resp.outcome == "OVERFUNDED"
