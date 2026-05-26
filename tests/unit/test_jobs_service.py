"""Integration-style tests for jobs.service.open_job + settle_job.

Mirrors the sessions tests but with the http-* mode set (cases
a/b/c). Verifies parallel behavior: worst-case encumbrance,
SDK-reported settlement, refund-unused on close.
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
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.jobs import service as jobs_service
from livepeer_open_clearinghouse.domains.jobs.service import (
    JobAlreadySettled,
    JobNotFound,
    ModeNotSupportedForJob,
)
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
    InvalidSessionRequest,
    ModeNotDeclared,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import InsufficientCredit, NoRouteAvailable
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


def _route(mode: str | None) -> SelectedRoute:
    extra = {"interaction_mode": mode} if mode is not None else {}
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
        extra=extra,
    )


# ---- open_job happy path ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_writes_session_with_http_mode(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
    assert resp.mode == "http-reqresp@v0"
    assert resp.settle_endpoint == f"/v1/jobs/{resp.job_id}/settle"
    # Worst case = 1000 x 100 = 100_000
    assert resp.funded_value_wei == 100_000
    # Initial mint is sized for FULL worst case (no refills for jobs)
    assert resp.expected_value_wei == 100_000

    ps = await db_session.get(PaymentSession, resp.job_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_OPEN
    assert ps.mode == "http-reqresp@v0"
    assert ps.max_total_units == 1000

    # Payment row tied to the session, funded for the full worst case.
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == resp.job_id))
    ).all()
    assert len(payments) == 1
    assert payments[0].funded_value_wei == Decimal(100_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_defaults_max_to_estimated(db_session: AsyncSession) -> None:
    """Case (a): SDK knows exactly what it needs; omits max_total_units."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
    registry = MockRegistryClient(routes=[_route("ws-realtime@v0")])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(ModeNotSupportedForJob):
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
async def test_open_job_rejects_mode_not_declared(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route(None)])
    daemon = MockPaymentDaemonClient()
    with pytest.raises(ModeNotDeclared):
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
async def test_open_job_rejects_max_below_estimated(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
async def test_settle_job_overfunded_refunds_unused(db_session: AsyncSession) -> None:
    """SDK opened with max_total=1000 (funded 100_000); broker actually
    processed 600 units. Settle: billed 60_000, refund 40_000."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
        outcome=None,
        settlement={"breakdown": {"prompt_tokens": 200, "completion_tokens": 400}},
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

    balance_after = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_after - balance_before == Decimal(40_000)


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
            outcome=None,
            settlement=None,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_rejects_second_call(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-reqresp@v0")])
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
        outcome=None,
        settlement=None,
        clock=_clock(),
        settings=_settings(),
    )
    with pytest.raises(JobAlreadySettled):
        await jobs_service.settle_job(
            db_session,
            job_id=open_resp.job_id,
            user_id=user_id,
            actual_units=10,
            outcome=None,
            settlement=None,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_settle_job_accepts_http_stream_mode(db_session: AsyncSession) -> None:
    """Case (c): http-stream@v0 — same shape as http-reqresp."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route("http-stream@v0")])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    resp = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=100,
        max_total_units=200,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert resp.mode == "http-stream@v0"
