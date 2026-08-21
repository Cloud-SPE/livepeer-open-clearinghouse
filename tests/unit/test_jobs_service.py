"""Integration-style tests for jobs.service.open_job + settle_job.

Mirrors the sessions tests for paid-job/v1 transports. Verifies
parallel behavior: worst-case encumbrance,
SDK-reported settlement, refund-unused on close.
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
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.jobs import service as jobs_service
from livepeer_open_clearinghouse.domains.jobs.service import (
    JobAlreadySettled,
    JobNotFound,
    ProtocolNotSupportedForJob,
    TransportNotSupportedForJob,
)
from livepeer_open_clearinghouse.domains.jobs.types import CreateJobResponse, SettlementEnvelope
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession, PaymentSettlement
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
    InvalidSessionRequest,
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
from tests.fixtures.signed_settlement import delegated_key, signed_job_settlement


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


async def _seed(db: AsyncSession, *, balance_wei: int = 10**12) -> tuple[uuid.UUID, uuid.UUID]:
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
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(balance_wei)))
    await db.flush()
    return user.id, key.id


def _route(protocol: str = "paid-job/v1") -> SelectedRoute:
    is_job = protocol == "paid-job/v1"
    extra = (
        {"job": {"transports": ["unary", "stream", "multipart"]}}
        if is_job
        else {
            "session": {
                "descriptor_schema": "test-runtime/v1",
                "metering": "runner-reported",
            }
        }
    )
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        price_per_work_unit_wei=Decimal(100),
        work_unit="token",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol=protocol,
        settlement_keys=(delegated_key(),),
        extra=extra,
    )


def _settlement(
    response: CreateJobResponse,
    *,
    broker_job_id: str,
    actual_units: int,
    debited_units: int | None = None,
    amount_wei: int = 100,
    per_units: int = 1,
    work_unit: str = "token",
    outcome: str = "OVERFUNDED",
) -> SettlementEnvelope:
    return SettlementEnvelope.model_validate(
        signed_job_settlement(
            request_id=response.request_id,
            job_id=broker_job_id,
            work_id=response.work_id,
            actual_units=actual_units,
            debited_units=debited_units,
            amount_wei=amount_wei,
            per_units=per_units,
            work_unit=work_unit,
            outcome=outcome,
        )
    )


# ---- open_job happy path ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_writes_session_with_http_mode(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=500,
        max_total_units=1000,  # post-settled, generous max
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert resp.protocol == "paid-job/v1"
    assert resp.transport == "unary"
    assert resp.work_unit == "token"
    assert resp.settle_endpoint == f"/v1/jobs/{resp.job_id}/settle"
    # Worst case = 1000 x 100 = 100_000
    assert resp.funded_value_wei == 100_000
    # Initial mint is sized for FULL worst case (no refills for jobs)
    assert resp.expected_value_wei == 100_000

    ps = await db_session.get(PaymentSession, resp.job_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_OPEN
    assert ps.protocol == "paid-job/v1"
    assert ps.broker_request_id == resp.request_id
    assert ps.route_snapshot is not None
    assert ps.route_snapshot["protocol"] == "paid-job/v1"
    assert ps.route_snapshot["axes"]["transports"] == [
        "unary",
        "stream",
        "multipart",
    ]
    assert ps.max_total_units == 1000

    # Payment row tied to the session, funded for the full worst case.
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == resp.job_id))
    ).all()
    assert len(payments) == 1
    assert payments[0].funded_value_wei == Decimal(100_000)
    assert payments[0].mint_request_id == f"loc:{resp.request_id}"
    assert payments[0].creation_round == 100
    assert payments[0].expires_after_round == 102


@pytest.mark.unit
@pytest.mark.asyncio
async def test_job_uses_cumulative_ceiling_for_non_unit_denominator(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    route = _route().model_copy(
        update={"price_per_work_unit_wei": Decimal(1), "units_per_price": 3}
    )
    response = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability=route.capability,
        offering=route.offering,
        estimated_units=4,
        max_total_units=4,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[route]),
        daemon=MockPaymentDaemonClient(ev_ratio=Decimal("1.0")),
        clock=_clock(),
        settings=_settings(),
    )
    assert response.funded_value_wei == 2  # ceil(4 x 1 / 3)

    settled = await jobs_service.settle_job(
        db_session,
        job_id=response.job_id,
        user_id=user_id,
        actual_units=1,
        broker_job_id="broker-job-denominator",
        work_unit="token",
        outcome=None,
        settlement=_settlement(
            response,
            broker_job_id="broker-job-denominator",
            actual_units=1,
            amount_wei=1,
            per_units=3,
        ),
        clock=_clock(),
        settings=_settings(),
    )
    assert settled.billed_value_wei == 1  # ceil(1 x 1 / 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_defaults_max_to_estimated(db_session: AsyncSession) -> None:
    """Case (a): SDK knows exactly what it needs; omits max_total_units."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=500,
        max_total_units=None,  # SDK omits → defaults to estimated
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    # funded = estimated x price = 500 x 100 = 50_000
    assert resp.funded_value_wei == 50_000


# ---- open_job error paths ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_session_mode(db_session: AsyncSession) -> None:
    """A ws-realtime offering can't be opened via /v1/jobs — use /v1/sessions."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("paid-session/v1")])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(ProtocolNotSupportedForJob):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_session_protocol(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("paid-session/v1")])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(ProtocolNotSupportedForJob):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_undeclared_transport_before_mint(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    route = _route().model_copy(update={"extra": {"job": {"transports": ["unary"]}}})
    with pytest.raises(TransportNotSupportedForJob) as exc_info:
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability=route.capability,
            offering=route.offering,
            transport="stream",
            estimated_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[route]),
            daemon=MockPaymentDaemonClient(),
            clock=_clock(),
            settings=_settings(),
        )
    assert exc_info.value.details == {
        "transport": "stream",
        "declared_transports": ["unary"],
    }
    assert (await db_session.scalars(select(Payment))).all() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_max_below_estimated(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(InvalidSessionRequest):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=100,
            max_total_units=50,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_invalid_envelope_expiry(db_session: AsyncSession) -> None:
    class InvalidExpiryDaemon(MockPaymentDaemonClient):
        async def create_payment(self, request):  # type: ignore[no-untyped-def]
            response = await super().create_payment(request)
            return replace(response, expires_after_round=response.creation_round)

    user_id, key_id = await _seed(db_session)
    with pytest.raises(DaemonUnavailable, match="invalid payment envelope expiry"):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[_route()]),
            daemon=InvalidExpiryDaemon(),
            clock=_clock(),
            settings=_settings(),
        )
    assert (await db_session.scalars(select(Payment))).all() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_no_route(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    daemon = MockPaymentDaemonClient()
    with pytest.raises(NoRouteAvailable):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="missing",
            offering="missing",
            estimated_units=1,
            max_total_units=1,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[]),
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_rejects_insufficient_balance(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session, balance_wei=50_000)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(InsufficientCredit):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=1000,  # 1000 x 100 = 100_000 > balance 50_000
            max_total_units=1000,
            sdk_identity=None,
            registry=registry,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


# ---- settle_job ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_keeps_encumbrance_when_broker_debit_failed(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    open_resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=100,
        max_total_units=100,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[_route()]),
        daemon=MockPaymentDaemonClient(ev_ratio=Decimal("1.0")),
        clock=_clock(),
        settings=_settings(),
    )
    balance_before = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    with pytest.raises(jobs_service.SettlementVerificationFailed) as exc_info:
        await jobs_service.settle_job(
            db_session,
            job_id=open_resp.job_id,
            user_id=user_id,
            actual_units=100,
            broker_job_id="broker-job-debit-failed",
            work_unit="token",
            outcome=None,
            settlement=_settlement(
                open_resp,
                broker_job_id="broker-job-debit-failed",
                actual_units=100,
                debited_units=0,
                outcome="DEBIT_FAILED",
            ),
            clock=_clock(),
            settings=_settings(),
        )

    assert exc_info.value.details == {"reason": "debit_failed"}
    row = await db_session.get(PaymentSession, open_resp.job_id)
    assert row is not None
    assert row.state == SESSION_STATE_OPEN
    balance_after = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_after == balance_before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_overfunded_refunds_unused(db_session: AsyncSession) -> None:
    """SDK opened with max_total=1000 (funded 100_000); broker actually
    processed 600 units. Settle: billed 60_000, refund 40_000."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    open_resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=800,
        max_total_units=1000,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    balance_before = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    settle_resp = await jobs_service.settle_job(
        db_session,
        job_id=open_resp.job_id,
        user_id=user_id,
        actual_units=600,
        broker_job_id="broker-job-overfunded",
        work_unit="token",
        outcome=None,
        settlement=_settlement(
            open_resp,
            broker_job_id="broker-job-overfunded",
            actual_units=600,
        ),
        clock=_clock(),
        settings=_settings(),
    )
    assert settle_resp.actual_units == 600
    assert settle_resp.billed_value_wei == 60_000
    assert settle_resp.refund_wei == 40_000
    assert settle_resp.outcome == "OVERFUNDED"
    # cap_status now ships with settle responses for portal UX.
    # session_pct_used = 60_000 / 100_000 = 0.6
    assert settle_resp.cap_status.session_pct_used == pytest.approx(0.6)
    assert settle_resp.cap_status.will_refuse_next_refill is False

    ps = await db_session.get(PaymentSession, open_resp.job_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_CLOSED
    assert ps.breakdown == {
        "broker_job_id": "broker-job-overfunded",
        "work_unit": "token",
    }
    event = await db_session.scalar(
        select(PaymentSettlement).where(PaymentSettlement.session_id == open_resp.job_id)
    )
    assert event is not None
    assert event.raw_record["broker_job_id"] == "broker-job-overfunded"
    assert event.raw_record["work_unit"] == "token"
    assert event.raw_record["settlement"]["payload"]["job_id"] == "broker-job-overfunded"

    balance_after = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_after - balance_before == Decimal(40_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_rejects_work_unit_drift_without_closing(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    open_resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=10,
        max_total_units=10,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[_route()]),
        daemon=MockPaymentDaemonClient(),
        clock=_clock(),
        settings=_settings(),
    )

    with pytest.raises(jobs_service.WorkUnitMismatch):
        await jobs_service.settle_job(
            db_session,
            job_id=open_resp.job_id,
            user_id=user_id,
            actual_units=10,
            broker_job_id="broker-job-drift",
            work_unit="frames",
            outcome=None,
            settlement=_settlement(
                open_resp,
                broker_job_id="broker-job-drift",
                actual_units=10,
            ),
            clock=_clock(),
            settings=_settings(),
        )

    job = await db_session.get(PaymentSession, open_resp.job_id)
    assert job is not None
    assert job.state == SESSION_STATE_OPEN
    assert (
        await db_session.scalar(
            select(PaymentSettlement).where(PaymentSettlement.session_id == open_resp.job_id)
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_rejects_tampered_signature_before_mutation(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed(db_session)
    open_resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=10,
        max_total_units=10,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[_route()]),
        daemon=MockPaymentDaemonClient(),
        clock=_clock(),
        settings=_settings(),
    )
    settlement = _settlement(open_resp, broker_job_id="broker-job-tampered", actual_units=10)
    settlement.payload["actual_units"] = "9"

    with pytest.raises(jobs_service.SettlementVerificationFailed):
        await jobs_service.settle_job(
            db_session,
            job_id=open_resp.job_id,
            user_id=user_id,
            actual_units=10,
            broker_job_id="broker-job-tampered",
            work_unit="token",
            outcome=None,
            settlement=settlement,
            clock=_clock(),
            settings=_settings(),
        )

    job = await db_session.get(PaymentSession, open_resp.job_id)
    assert job is not None
    assert job.state == SESSION_STATE_OPEN
    assert (
        await db_session.scalar(
            select(PaymentSettlement).where(PaymentSettlement.session_id == open_resp.job_id)
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_rejects_unknown(db_session: AsyncSession) -> None:
    user_id, _ = await _seed(db_session)
    with pytest.raises(JobNotFound):
        await jobs_service.settle_job(
            db_session,
            job_id=uuid.uuid4(),
            user_id=user_id,
            actual_units=0,
            broker_job_id="broker-job-missing",
            work_unit="token",
            outcome=None,
            settlement=SettlementEnvelope.model_validate(
                signed_job_settlement(
                    job_id="broker-job-missing",
                    work_id="missing",
                    actual_units=0,
                    amount_wei=100,
                    per_units=1,
                )
            ),
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_rejects_second_call(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient()
    open_resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=10,
        max_total_units=10,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    await jobs_service.settle_job(
        db_session,
        job_id=open_resp.job_id,
        user_id=user_id,
        actual_units=10,
        broker_job_id="broker-job-once",
        work_unit="token",
        outcome=None,
        settlement=_settlement(
            open_resp,
            broker_job_id="broker-job-once",
            actual_units=10,
            outcome="EXACT",
        ),
        clock=_clock(),
        settings=_settings(),
    )
    with pytest.raises(JobAlreadySettled):
        await jobs_service.settle_job(
            db_session,
            job_id=open_resp.job_id,
            user_id=user_id,
            actual_units=10,
            broker_job_id="broker-job-twice",
            work_unit="token",
            outcome=None,
            settlement=_settlement(
                open_resp,
                broker_job_id="broker-job-twice",
                actual_units=10,
                outcome="EXACT",
            ),
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_accepts_stream_transport(db_session: AsyncSession) -> None:
    """A stream job uses the same settlement shape as a unary job."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        transport="stream",
        estimated_units=100,
        max_total_units=200,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert resp.protocol == "paid-job/v1"
