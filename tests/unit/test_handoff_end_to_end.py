"""End-to-end demo: open_job + mock-broker call + settle_job.

Composes the real LOC service code (sessions.service.open_job + jobs.service.settle_job)
against the in-tree mock broker fixture. Verifies the complete handoff
flow lands the right Payment + payment_session + settlement rows, the
balance is encumbered and released correctly, and the response carries
the broker's actual_units.

This is the demo we'd run to convince ourselves the loop actually
closes before shipping the SDKs against a real orchestrator.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
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
from livepeer_open_clearinghouse.domains.jobs.types import SettlementEnvelope
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.sessions.service import SESSION_STATE_CLOSED
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    MockRegistryClient,
    SelectedRoute,
)
from livepeer_open_clearinghouse.settings import Settings
from tests.fixtures.mock_broker import build_mock_broker_app
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


def _route(broker_url: str) -> SelectedRoute:
    return SelectedRoute(
        worker_url=broker_url,
        eth_address="0x" + "11" * 20,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        price_per_work_unit_wei=Decimal(1000),
        work_unit="token",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol="paid-job/v1",
        settlement_keys=(delegated_key(),),
        extra={"job": {"transports": ["unary", "stream", "multipart"]}},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_job_then_call_mock_broker_then_settle(
    db_session: AsyncSession,
) -> None:
    """The full handoff loop without the network. End-to-end proof that:

    1. open_job mints + returns a broker_url + payment_envelope
    2. Calling the mock broker with that envelope works (header
       round-trip + actual_units returned via Livepeer-Work-Units)
    3. settle_job records the right billed/refund/outcome and
       releases the encumbered balance correctly
    """
    user_id, key_id = await _seed(db_session)
    balance_before = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei

    # Spin up the mock broker as an in-process ASGI app
    broker_app = build_mock_broker_app(default_units=120)
    broker_base = "http://mock-broker"

    registry = MockRegistryClient(routes=[_route(broker_base)])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    # 1. Open the job (mints + returns broker_url + envelope)
    job = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=200,
        max_total_units=200,  # case (a): SDK knows exact size
        sdk_identity="python/test/handoff",
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert job.broker_url == broker_base
    assert job.payment_envelope  # base64-encoded mock payment

    # 2. Call the mock broker with the minted envelope (SDK's job)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=broker_app),
        base_url=broker_base,
    ) as broker_client:
        resp = await broker_client.post(
            "/v1/job",
            headers={
                "Livepeer-Capability": "openai:chat-completions",
                "Livepeer-Offering": "gpt-oss-20b",
                "Livepeer-Payment": job.payment_envelope,
                "Livepeer-Protocol": job.protocol,
                "Livepeer-Request-Id": job.request_id,
                "Content-Type": "application/json",
            },
            json={"prompt": "hello"},
        )
    assert resp.status_code == 200
    assert resp.headers["Livepeer-Work-Units"] == "120"
    body = resp.json()
    assert body["usage"]["actual_units"] == 120

    # Verify the broker actually received the envelope (handoff intact)
    broker_request = broker_app.state.requests[0]
    assert broker_request["had_payment_header"] is True
    assert broker_request["capability"] == "openai:chat-completions"
    assert broker_request["protocol"] == "paid-job/v1"
    # Decode the envelope and check the magic prefix from MockPaymentDaemonClient
    decoded = base64.b64decode(job.payment_envelope)
    assert decoded.startswith(b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT")

    # 3. Settle with the actual_units read from the broker's header
    settle = await jobs_service.settle_job(
        db_session,
        job_id=job.job_id,
        user_id=user_id,
        actual_units=120,
        broker_job_id="broker-job-handoff",
        work_unit="token",
        outcome=None,
        settlement=SettlementEnvelope.model_validate(
            signed_job_settlement(
                job_id="broker-job-handoff",
                work_id=job.work_id,
                actual_units=120,
                amount_wei=1000,
                per_units=1,
            )
        ),
        clock=_clock(),
        settings=_settings(),
    )
    # max_total_units=200 x price 1000 = 200_000 encumbered
    # actual 120 x 1000 = 120_000 billed
    # refund 80_000
    assert settle.actual_units == 120
    assert settle.billed_value_wei == 120_000
    assert settle.refund_wei == 80_000
    assert settle.outcome == "OVERFUNDED"
    assert settle.cap_status.session_pct_used == pytest.approx(0.6)

    # Session row finalized
    session_row = await db_session.get(PaymentSession, job.job_id)
    assert session_row is not None
    assert session_row.state == SESSION_STATE_CLOSED
    assert session_row.actual_units == 120
    assert session_row.outcome == "OVERFUNDED"

    # Payment row was written and linked
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == job.job_id))
    ).all()
    assert len(payments) == 1

    # Close settlement event written
    events = (
        await db_session.scalars(
            select(PaymentSettlement).where(
                PaymentSettlement.session_id == job.job_id,
                PaymentSettlement.event_type == "close",
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].outcome == "OVERFUNDED"

    # Balance: encumbered 200_000, refunded 80_000 → net -120_000
    balance_after = (await billing_service.get_balance(db_session, user_id=user_id)).amount_wei
    assert balance_before - balance_after == Decimal(120_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_broker_returns_configurable_actual_units(
    db_session: AsyncSession,
) -> None:
    """The mock broker honors X-Mock-Actual-Units for tests that want
    to drive specific accounting outcomes (EXACT, UNDERFUNDED, etc.)."""
    user_id, key_id = await _seed(db_session)
    broker_app = build_mock_broker_app(default_units=50)
    broker_base = "http://mock-broker"

    registry = MockRegistryClient(routes=[_route(broker_base)])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))

    job = await jobs_service.open_job(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        estimated_units=100,
        max_total_units=100,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=broker_app),
        base_url=broker_base,
    ) as broker_client:
        # Override default_units=50 with the EXACT-match value 100
        resp = await broker_client.post(
            "/v1/job",
            headers={
                "Livepeer-Payment": job.payment_envelope,
                "Livepeer-Protocol": job.protocol,
                "Livepeer-Request-Id": job.request_id,
                "X-Mock-Actual-Units": "100",
                "Content-Type": "application/json",
            },
            json={},
        )
    assert resp.headers["Livepeer-Work-Units"] == "100"
    settle = await jobs_service.settle_job(
        db_session,
        job_id=job.job_id,
        user_id=user_id,
        actual_units=100,
        broker_job_id="broker-job-error",
        work_unit="token",
        outcome=None,
        settlement=SettlementEnvelope.model_validate(
            signed_job_settlement(
                job_id="broker-job-error",
                work_id=job.work_id,
                actual_units=100,
                amount_wei=1000,
                per_units=1,
                outcome="EXACT",
            )
        ),
        clock=_clock(),
        settings=_settings(),
    )
    # 100 x 1000 = 100_000 billed; funded 100_000; outcome EXACT
    assert settle.outcome == "EXACT"
    assert settle.refund_wei == 0
