"""A refused or abandoned initial authorization never strands a customer hold.

The create path commits the engagement claim (session row + customer hold)
before asking the payer to sign, so a crashed request can be replayed. These
tests pin the recovery rules around that commit: a definitive payer refusal
closes the claim and releases the hold, a transient failure keeps it for the
replay, the janitor releases claims whose request died, and a released claim
can never be authorized afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import grpc
import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditLedger
from livepeer_open_clearinghouse.domains.jobs import service as jobs_service
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    SpendAuthorizationGrant,
)
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
)
from livepeer_open_clearinghouse.domains.wholesale import service as wholesale_service
from livepeer_open_clearinghouse.domains.wholesale.repo import WholesaleExposureBudget
from livepeer_open_clearinghouse.errors import (
    AuthorizationRefused,
    EngagementClosed,
    WholesaleFundingUnverified,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import (
    GrpcPaymentDaemonClient,
    MockPaymentDaemonClient,
    PaymentDaemonError,
    SpendAuthorizationRefused,
)
from livepeer_open_clearinghouse.providers.registry_daemon.client import MockRegistryClient
from tests.unit.test_jobs_service import _route as _job_route
from tests.unit.test_jobs_service import _seed as _seed_job_user
from tests.unit.test_jobs_service import _WholesaleBroker as _JobBroker
from tests.unit.test_open_session_service import (
    _clock,
    _route_for_protocol,
    _seed_user_key_and_balance,
    _settings,
    _WholesaleBroker,
)
from tests.unit.test_payment_daemon_grpc_mapping import _sample_authorization_request

_CALLER = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
_REFUSAL = (
    "CreateSpendAuthorization FAILED_PRECONDITION: spend limit: max_debit "
    "1200000000000000 wei exceeds max-authorization-wei 1000000000000000"
)


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


class _RefusingDaemon(MockPaymentDaemonClient):
    async def create_spend_authorization(self, request):  # type: ignore[no-untyped-def]
        raise SpendAuthorizationRefused(_REFUSAL)


class _FlakyDaemon(MockPaymentDaemonClient):
    async def create_spend_authorization(self, request):  # type: ignore[no-untyped-def]
        raise PaymentDaemonError("CreateSpendAuthorization UNAVAILABLE: socket closed")


def _wholesale_settings():  # type: ignore[no-untyped-def]
    return _settings().model_copy(
        update={
            "wholesale_chain_id": 42161,
            "wholesale_target_available_wei": 100,
            "wholesale_replenish_below_wei": 50,
            "wholesale_max_available_per_payee_wei": 200,
            "wholesale_max_aggregate_available_wei": 500,
            "wholesale_max_single_funding_wei": 100,
        }
    )


async def _open_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    daemon: MockPaymentDaemonClient,
    request_id: str = "session-refusal-1",
    clock: FrozenClock | None = None,
    broker: _WholesaleBroker | None = None,
) -> sessions_service.CreateSessionResponse:
    route = _route_for_protocol("paid-session/v1")
    clock = clock or _clock()
    settings = _wholesale_settings().model_copy(
        update={
            "wholesale_target_available_wei": 10_000,
            "wholesale_max_available_per_payee_wei": 20_000,
            "wholesale_max_aggregate_available_wei": 50_000,
            "wholesale_max_single_funding_wei": 10_000,
        }
    )
    prepared = await sessions_service.prepare_session(
        user_id=user_id,
        api_key_id=key_id,
        capability=route.capability,
        offering=route.offering,
        descriptor_schema="test-runtime/v1",
        route_binding=None,
        registry=MockRegistryClient(routes=[route]),
        clock=clock,
        settings=settings,
    )
    return await sessions_service.open_session(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability=route.capability,
        offering=route.offering,
        descriptor_schema="test-runtime/v1",
        estimated_runway_units=2,
        max_total_units=10,
        gateway_session_id=prepared.gateway_session_id,
        preparation_token=prepared.preparation_token,
        route_binding=prepared.route_binding,
        sdk_identity=None,
        registry=MockRegistryClient(routes=[route]),
        daemon=daemon,
        clock=clock,
        settings=settings,
        request_id=request_id,
        workload_request_digest=b"\x55" * 32,
        caller_public_key=_CALLER,
        broker_wholesale=broker or _WholesaleBroker(),
    )


async def _seed_session_user(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, key_id = await _seed_user_key_and_balance(db, balance_wei=100_000)
    db.add(WholesaleExposureBudget(scope="global", projected_available_wei=Decimal(0)))
    await db.commit()
    return user_id, key_id


async def _ledger_reasons(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await db.scalars(
        select(CreditLedger.reason)
        .where(CreditLedger.user_id == user_id)
        .order_by(CreditLedger.created_at)
    )
    return list(rows.all())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refused_session_authorization_releases_the_hold(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed_session_user(db_session)

    with pytest.raises(AuthorizationRefused) as refused:
        await _open_session(db_session, user_id=user_id, key_id=key_id, daemon=_RefusingDaemon())

    assert refused.value.status_code == 422
    assert "max-authorization-wei" in refused.value.details["reason"]
    session = (await db_session.scalars(select(PaymentSession))).one()
    assert session.state == SESSION_STATE_CLOSED
    assert session.outcome == "NOT_ADMITTED"
    assert session.authorization_id is None
    assert session.billed_value_wei == Decimal(0)
    balance = await billing_service.get_balance(db_session, user_id=user_id)
    assert balance.amount_wei == Decimal(100_000)
    assert "engagement_release" in await _ledger_reasons(db_session, user_id)

    # A replay of the same request must not resurrect the released claim.
    with pytest.raises(EngagementClosed):
        await _open_session(
            db_session, user_id=user_id, key_id=key_id, daemon=MockPaymentDaemonClient()
        )
    assert (await db_session.scalars(select(SpendAuthorizationGrant))).all() == []
    balance = await billing_service.get_balance(db_session, user_id=user_id)
    assert balance.amount_wei == Decimal(100_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transient_authorization_failure_keeps_the_claim_for_replay(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_session_user(db_session)

    with pytest.raises(PaymentDaemonError) as failed:
        await _open_session(db_session, user_id=user_id, key_id=key_id, daemon=_FlakyDaemon())
    assert not isinstance(failed.value, SpendAuthorizationRefused)

    session = (await db_session.scalars(select(PaymentSession))).one()
    assert session.state == SESSION_STATE_OPEN
    held = await billing_service.get_balance(db_session, user_id=user_id)
    assert held.amount_wei == Decimal(90_000)

    response = await _open_session(
        db_session, user_id=user_id, key_id=key_id, daemon=MockPaymentDaemonClient()
    )
    assert response.session_id == session.id
    after = await billing_service.get_balance(db_session, user_id=user_id)
    assert after.amount_wei == Decimal(90_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_release_racing_the_grant_rejects_the_authorization(
    db_session: AsyncSession,
) -> None:
    user_id, key_id = await _seed_session_user(db_session)

    class _JanitorWinsDaemon(MockPaymentDaemonClient):
        async def create_spend_authorization(self, request):  # type: ignore[no-untyped-def]
            response = await super().create_spend_authorization(request)
            engagement_id = uuid.UUID(request.session_id)
            async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as other:
                assert await sessions_service.release_unauthorized_engagement(
                    other, engagement_id=engagement_id, clock=_clock()
                )
            return response

    with pytest.raises(EngagementClosed):
        await _open_session(db_session, user_id=user_id, key_id=key_id, daemon=_JanitorWinsDaemon())
    await db_session.rollback()
    assert (await db_session.scalars(select(SpendAuthorizationGrant))).all() == []
    balance = await billing_service.get_balance(db_session, user_id=user_id)
    assert balance.amount_wei == Decimal(100_000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_releases_only_stale_unauthorized_claims(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed_session_user(db_session)
    route = _route_for_protocol("paid-session/v1")
    base = _clock()

    async def claim(request_id: str, clock: FrozenClock) -> PaymentSession:
        return await sessions_service.claim_wholesale_engagement(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            route=route,
            broker_request_id=request_id,
            estimated_units=1,
            max_total_units=1,
            max_debit_wei=Decimal(1_000),
            clock=clock,
            sdk_identity=None,
            spend_period_seconds=86_400,
            spend_period_cap_wei=0,
        )

    stale = await claim("stale", base)
    fresh = await claim("fresh", FrozenClock(base.now() + timedelta(minutes=9)))
    authorized = await _open_session(
        db_session, user_id=user_id, key_id=key_id, daemon=MockPaymentDaemonClient()
    )

    released = await sessions_service.release_stale_unauthorized_engagements(
        db_session,
        clock=FrozenClock(base.now() + timedelta(minutes=10)),
        older_than_seconds=300,
    )

    assert released == 1
    rows = {r.id: r for r in (await db_session.scalars(select(PaymentSession))).all()}
    assert rows[stale.id].state == SESSION_STATE_CLOSED
    assert rows[stale.id].outcome == "NOT_ADMITTED"
    assert rows[fresh.id].state == SESSION_STATE_OPEN
    assert rows[authorized.session_id].state == SESSION_STATE_OPEN
    again = await sessions_service.release_stale_unauthorized_engagements(
        db_session,
        clock=FrozenClock(base.now() + timedelta(minutes=10)),
        older_than_seconds=300,
    )
    assert again == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unproven_session_funding_is_a_retryable_503(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, key_id = await _seed_session_user(db_session)

    async def unproven(*_: object, **__: object) -> None:
        raise wholesale_service.WholesaleFundingPolicyError(
            "broker funding replay is not proven by durable account credit"
        )

    monkeypatch.setattr(sessions_service, "_replenish_wholesale_account", unproven)
    with pytest.raises(WholesaleFundingUnverified) as unverified:
        await _open_session(
            db_session, user_id=user_id, key_id=key_id, daemon=MockPaymentDaemonClient()
        )
    assert unverified.value.status_code == 503
    # The authorization is durable, so the same request can resume funding.
    session = (await db_session.scalars(select(PaymentSession))).one()
    assert session.state == SESSION_STATE_OPEN
    assert session.authorization_id is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refused_job_authorization_releases_the_hold(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed_job_user(db_session)
    route = _job_route()
    db_session.add(WholesaleExposureBudget(scope="global", projected_available_wei=Decimal(0)))
    await db_session.commit()
    before = await billing_service.get_balance(db_session, user_id=user_id)
    starting = before.amount_wei

    with pytest.raises(AuthorizationRefused):
        await jobs_service.open_job(
            db_session,
            user_id=user_id,
            api_key_id=key_id,
            capability=route.capability,
            offering=route.offering,
            estimated_units=1,
            max_total_units=2,
            sdk_identity=None,
            registry=MockRegistryClient(routes=[route]),
            daemon=_RefusingDaemon(),
            clock=_clock(),
            settings=_wholesale_settings(),
            request_id="job-refusal-1",
            workload_request_digest=b"\x44" * 32,
            caller_public_key=_CALLER,
            broker_wholesale=_JobBroker(),
        )

    job = (await db_session.scalars(select(PaymentSession))).one()
    assert job.state == SESSION_STATE_CLOSED
    assert job.outcome == "NOT_ADMITTED"
    after = await billing_service.get_balance(db_session, user_id=user_id)
    assert after.amount_wei == starting


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "refused"),
    [
        (grpc.StatusCode.FAILED_PRECONDITION, True),
        (grpc.StatusCode.INVALID_ARGUMENT, True),
        (grpc.StatusCode.PERMISSION_DENIED, True),
        (grpc.StatusCode.UNAVAILABLE, False),
        (grpc.StatusCode.DEADLINE_EXCEEDED, False),
        (grpc.StatusCode.INTERNAL, False),
    ],
)
async def test_only_definitive_statuses_map_to_refusal(
    status: grpc.StatusCode, refused: bool
) -> None:
    class Stub:
        async def CreateSpendAuthorization(self, _request: object) -> object:
            raise grpc.aio.AioRpcError(
                status, grpc.aio.Metadata(), grpc.aio.Metadata(), details="payer said no"
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    with pytest.raises(PaymentDaemonError) as raised:
        await client.create_spend_authorization(_sample_authorization_request())
    assert isinstance(raised.value, SpendAuthorizationRefused) is refused
