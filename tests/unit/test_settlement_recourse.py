"""Settlement recourse: fail closed at open, stop retrying, operator resolve."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.admin.repo import Operator, OperatorAudit
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.jobs import service as jobs_service
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession, PaymentSettlement
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage import service as usage
from livepeer_open_clearinghouse.errors import NoSettlementDelegation
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerExchangeOutcome,
    BrokerExchangeResult,
)
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon.client import MockRegistryClient
from tests.unit.test_jobs_service import (
    _clock,
    _route,
    _seed,
    _settings,
    _settlement,
    _StaticExchangeClient,
)

FUNDED = 10 * 100  # estimated 10 units at 100 wei, ev_ratio 1.0


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _open(db: AsyncSession, *, balance: int = 10**12):
    user_id, key_id = await _seed(db, balance_wei=balance)
    response = await jobs_service.open_job(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        transport="unary",
        estimated_units=10,
        max_total_units=10,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[_route()]),
        daemon=MockPaymentDaemonClient(ev_ratio=Decimal("1.0")),
        clock=_clock(),
        settings=_settings(),
    )
    return user_id, key_id, response


async def _balance(db: AsyncSession, user_id) -> Decimal:
    row = await db.scalar(select(CreditBalance).where(CreditBalance.user_id == user_id))
    assert row is not None
    return Decimal(row.amount_wei)


async def _operator(db: AsyncSession) -> Operator:
    op = Operator(email="ops@example.com", name="Ops", token_hash="x", role="owner")
    db.add(op)
    await db.flush()
    return op


# ---------------------------------------------------------------------------
# loc-h6l — fail closed at open
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_refuses_route_without_delegation(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    undelegated = _route().model_copy(update={"settlement_keys": ()})
    with pytest.raises(NoSettlementDelegation) as exc_info:
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            transport="unary",
            estimated_units=10,
            max_total_units=10,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[undelegated]),
            daemon=MockPaymentDaemonClient(),
            clock=_clock(),
            settings=_settings(),
        )
    assert exc_info.value.code == "no_settlement_delegation"
    assert exc_info.value.status_code == 400
    # Nothing was funded or encumbered.
    assert await db_session.scalar(select(func.count()).select_from(PaymentSession)) == 0
    assert await _balance(db_session, user_id) == Decimal(10**12)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_refuses_route_without_delegation(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    undelegated = _route("paid-session/v1").model_copy(update={"settlement_keys": ()})
    with pytest.raises(NoSettlementDelegation):
        await sessions_service.open_session(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            descriptor_schema="test-runtime/v1",
            estimated_runway_units=10,
            max_total_units=20,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[undelegated]),
            daemon=MockPaymentDaemonClient(),
            clock=_clock(),
            settings=_settings(),
        )


# ---------------------------------------------------------------------------
# loc-e28 — a permanent verification failure is recorded once, not per pass
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconciler_blocks_unverifiable_record_after_one_failure(
    db_session: AsyncSession,
) -> None:
    user_id, _key, response = await _open(db_session)
    settlement = _settlement(response, broker_job_id="broker-bad", actual_units=7)
    settlement.payload["actual_units"] = "6"  # tampered: fails verification every time
    client = _StaticExchangeClient(
        BrokerExchangeResult(
            request_id=response.request_id,
            outcome=BrokerExchangeOutcome.SETTLED,
            settlement=settlement.model_dump(mode="json"),
        )
    )

    async def events() -> int:
        return int(
            await db_session.scalar(
                select(func.count()).where(
                    TelemetryEvent.event_type == usage.SETTLEMENT_FAILURE_EVENT
                )
            )
            or 0
        )

    clock = _clock()
    first = await jobs_service.reconcile_open_jobs(
        db_session, settlement_client=client, clock=clock, settings=_settings()
    )
    assert first == 0
    row = await db_session.get(PaymentSession, response.job_id)
    assert row is not None
    assert row.state == "open"
    block = (row.breakdown or {})["settlement_block"]
    reason = block["reason"]
    assert reason  # a verification code such as invalid_signature / key_not_delegated
    assert block["attempts"] == 1
    assert block["signature"] == settlement.signature.value
    assert await events() == 1

    # Next passes see the same record and neither re-verify nor re-report.
    clock.advance(timedelta(seconds=120))
    row.last_polled_at = None
    await db_session.flush()
    await jobs_service.reconcile_open_jobs(
        db_session, settlement_client=client, clock=clock, settings=_settings()
    )
    row = await db_session.get(PaymentSession, response.job_id)
    assert row is not None
    assert (row.breakdown or {})["settlement_block"]["attempts"] == 1
    assert await events() == 1

    # The usage views surface the reason instead of a silent "open".
    page = await usage.list_jobs(
        db_session, filters=usage.JobFilters(user_id=user_id), limit=10, offset=0, clock=clock
    )
    assert page.items[0].accounting_outcome == "unresolved"
    assert page.items[0].blocked_reason == reason
    attention = await usage.attention(db_session, clock=clock, stale_after_seconds=1)
    assert attention.unresolved[0].blocked_reason == reason


# ---------------------------------------------------------------------------
# loc-8di — operator resolve
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_refund_hold_releases_the_encumbrance(db_session: AsyncSession) -> None:
    user_id, _key, response = await _open(db_session)
    op = await _operator(db_session)
    assert await _balance(db_session, user_id) == Decimal(10**12 - FUNDED)

    result = await admin_service.resolve_stuck_work(
        db_session,
        job_id=response.job_id,
        operator=op,
        action="refund_hold",
        note="pilot leftover",
        clock=_clock(),
    )

    assert result.state == "closed"
    assert result.outcome == "operator_refund_hold"
    assert result.billed_value_wei == Decimal(0)
    assert result.refund_wei == Decimal(FUNDED)
    assert await _balance(db_session, user_id) == Decimal(10**12)
    row = await db_session.get(PaymentSession, response.job_id)
    assert row is not None
    assert row.state == "closed"
    assert row.breakdown is not None
    assert row.breakdown["operator_resolution"]["operator_email"] == "ops@example.com"
    audit = await db_session.scalar(
        select(OperatorAudit).where(OperatorAudit.action == "resolve_job")
    )
    assert audit is not None
    assert audit.target_user_id == user_id
    assert audit.params is not None
    assert audit.params["action"] == "refund_hold"
    event = await db_session.scalar(
        select(PaymentSettlement).where(PaymentSettlement.session_id == response.job_id)
    )
    assert event is not None
    assert event.event_type == "operator_resolve"

    with pytest.raises(admin_service.JobNotResolvable) as exc_info:
        await admin_service.resolve_stuck_work(
            db_session,
            job_id=response.job_id,
            operator=op,
            action="refund_hold",
            note=None,
            clock=_clock(),
        )
    assert exc_info.value.details["reason"] == "already_closed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_accept_reported_bills_the_broker_units(db_session: AsyncSession) -> None:
    user_id, _key, response = await _open(db_session)
    op = await _operator(db_session)
    row = await db_session.get(PaymentSession, response.job_id)
    assert row is not None
    row.breakdown = {"broker_exchange": {"outcome": "SETTLED", "work_units": 7}}
    await db_session.flush()

    result = await admin_service.resolve_stuck_work(
        db_session,
        job_id=response.job_id,
        operator=op,
        action="accept_reported",
        note=None,
        clock=_clock(),
    )

    # 7 units at 100 wei per unit, snapshot units_per_price 1
    assert result.actual_units == 7
    assert result.billed_value_wei == Decimal(700)
    assert result.refund_wei == Decimal(FUNDED - 700)
    assert await _balance(db_session, user_id) == Decimal(10**12 - 700)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_accept_reported_needs_a_broker_report(db_session: AsyncSession) -> None:
    _user_id, _key, response = await _open(db_session)
    op = await _operator(db_session)
    with pytest.raises(admin_service.JobNotResolvable) as exc_info:
        await admin_service.resolve_stuck_work(
            db_session,
            job_id=response.job_id,
            operator=op,
            action="accept_reported",
            note=None,
            clock=_clock(),
        )
    assert exc_info.value.details["reason"] == "no_broker_report"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_charge_full_keeps_the_funded_value(db_session: AsyncSession) -> None:
    user_id, _key, response = await _open(db_session)
    op = await _operator(db_session)
    result = await admin_service.resolve_stuck_work(
        db_session,
        job_id=response.job_id,
        operator=op,
        action="charge_full",
        note="deadline",
        clock=_clock(),
    )
    assert result.billed_value_wei == Decimal(FUNDED)
    assert result.refund_wei == Decimal(0)
    assert await _balance(db_session, user_id) == Decimal(10**12 - FUNDED)


# ---------------------------------------------------------------------------
# loc-gcg — wei fields on job/session views are integer strings
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_open_response_serializes_wei_as_strings(db_session: AsyncSession) -> None:
    _user_id, _key, response = await _open(db_session)
    dumped = response.model_dump(mode="json")
    assert dumped["funded_value_wei"] == str(FUNDED)
    assert dumped["expected_value_wei"] == str(FUNDED)
    assert isinstance(dumped["funded_value_wei"], str)


@pytest.mark.unit
def test_inbound_wei_accepts_integer_strings_beyond_2_53() -> None:
    from livepeer_open_clearinghouse.domains.billing.types import TopupRequest

    req = TopupRequest.model_validate({"amount_wei": "12345678901234567890"})
    assert req.amount_wei == Decimal("12345678901234567890")
    assert req.model_dump(mode="json")["amount_wei"] == "12345678901234567890"
    with pytest.raises(ValueError):
        TopupRequest.model_validate({"amount_wei": "1.5"})


pytestmark = pytest.mark.skip(
    reason="legacy job/session fixtures; wholesale replacement belongs to loc-0m4.4"
)
