"""Integration-style tests for sessions.service.refill_session.

Opens a session via open_session, then exercises refills across the
happy path + each refusal branch (state, refill declaration, session cap,
spend-period cap). Also covers the will_refuse_next_refill /
winddown_reason fields on the cap_status block.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.sessions.runtime import refill_session_endpoint
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
    InvalidSessionRequest,
    RefillNotSupported,
    SessionCapReached,
    SessionNotFound,
    SessionNotOpen,
)
from livepeer_open_clearinghouse.domains.sessions.types import RefillSessionRequest
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    OpenClearinghouseError,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    MintOutcomeUnknown,
    MockPaymentDaemonClient,
    PaymentDaemonError,
)
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    MockRegistryClient,
    SelectedRoute,
    SettlementKey,
)
from livepeer_open_clearinghouse.settings import Settings
from tests.fixtures.signed_settlement import delegated_key, signed_session_settlement


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


async def _seed(db: AsyncSession, *, balance_wei: int = 10**18) -> tuple[uuid.UUID, uuid.UUID]:
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


def _route(
    *, refill: str = "extensible", price_wei: int = 1000, per_units: int = 1
) -> SelectedRoute:
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        price_per_work_unit_wei=Decimal(price_wei),
        work_unit="session_second",
        units_per_price=per_units,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol="paid-session/v1",
        settlement_keys=(SettlementKey.model_validate(delegated_key()),),
        extra={
            "session": {
                "descriptor_schema": "test-runtime/v1",
                "metering": "runner-reported",
                "refill": refill,
            }
        },
    )


async def _open_extensible_session(
    db: AsyncSession,
    *,
    estimated: int = 100,
    max_total: int = 1000,
    price_wei: int = 1000,
    per_units: int = 1,
    balance_wei: int = 10**18,
    daemon: MockPaymentDaemonClient | None = None,
):
    user_id, key_id = await _seed(db, balance_wei=balance_wei)
    registry = MockRegistryClient(routes=[_route(price_wei=price_wei, per_units=per_units)])
    daemon = daemon or MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    open_resp = await sessions_service.open_session(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        estimated_runway_units=estimated,
        max_total_units=max_total,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    return user_id, key_id, open_resp, daemon


class LoseNextCompletedResponseDaemon(MockPaymentDaemonClient):
    def __init__(self) -> None:
        super().__init__(ev_ratio=Decimal("1.0"))
        self.lose_next = False
        self.attempts = 0

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        self.attempts += 1
        response = await super().create_payment(request)
        if self.lose_next:
            self.lose_next = False
            raise PaymentDaemonError("response lost after durable payer completion")
        return response


class IncompleteNextReservationDaemon(MockPaymentDaemonClient):
    def __init__(self) -> None:
        super().__init__(ev_ratio=Decimal("1.0"))
        self.fail_next = False
        self.attempts = 0

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        self.attempts += 1
        if self.fail_next:
            self.fail_next = False
            raise MintOutcomeUnknown("mint was reserved but never completed")
        return await super().create_payment(request)


# ---- happy path ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_writes_new_payment_and_bumps_debit_seq(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(
        db_session, estimated=100, max_total=1000, price_wei=1000
    )

    refill_resp = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=80,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    assert refill_resp.work_id == open_resp.work_id
    assert refill_resp.refill_seq == 1
    assert refill_resp.payment_envelope
    # refill chunk = estimated runway x price = 100 x 1000 = 100_000
    assert refill_resp.funded_value_wei == 100_000
    assert refill_resp.expected_value_wei == 100_000

    # cap_status: session_pct_used = (initial 100_000 + refill 100_000) / 1_000_000 = 0.2
    assert refill_resp.cap_status.session_pct_used == pytest.approx(0.2)
    assert refill_resp.cap_status.will_refuse_next_refill is False
    assert refill_resp.cap_status.winddown_reason is None

    # Two Payments tied to the session now (initial + refill)
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
    ).all()
    assert len(payments) == 2
    assert all(payment.mint_request_id for payment in payments)
    assert payments[0].mint_request_id != payments[1].mint_request_id

    # Session's LOC-side refill ordinal bumped
    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.refill_seq == 1

    # Settlement event written
    events = (
        await db_session.scalars(
            select(PaymentSettlement).where(PaymentSettlement.session_id == open_resp.session_id)
        )
    ).all()
    assert any(e.event_type == "refill_granted" for e in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_funding_uses_cumulative_ceiling_delta(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(
        db_session,
        estimated=1,
        max_total=3,
        price_wei=1,
        per_units=3,
    )

    first = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=None,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    second = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=None,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )

    # bill(1)=bill(2)=bill(3)=1 wei. Independent rounding would fund
    # three wei; cumulative deltas fund one wei over the whole session.
    assert first.funded_value_wei == 0
    assert second.funded_value_wei == 0
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
    ).all()
    assert sum(int(payment.funded_value_wei) for payment in payments) == 1
    assert second.cap_status.will_refuse_next_refill is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_endpoint_replays_one_payer_mint_and_accounting_mutation(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    body = RefillSessionRequest(observed_consumed_units=80)

    first = await refill_session_endpoint(
        session_id=open_resp.session_id,
        body=body,
        pair=(api_key, user),
        db=db_session,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        idempotency_key="stable-refill-1",
    )
    await db_session.commit()
    replay = await refill_session_endpoint(
        session_id=open_resp.session_id,
        body=body,
        pair=(api_key, user),
        db=db_session,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        idempotency_key="stable-refill-1",
    )

    assert replay == first
    assert first.request_id
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
    ).all()
    assert len(payments) == 2
    assert payments[-1].mint_request_id == f"loc:{first.request_id}"
    session_row = await db_session.get(PaymentSession, open_resp.session_id)
    assert session_row is not None
    assert session_row.refill_seq == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_endpoint_rejects_changed_payload_under_same_key(
    db_session: AsyncSession,
) -> None:
    from livepeer_open_clearinghouse.errors import IdempotencyKeyReuse

    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    arguments = {
        "session_id": open_resp.session_id,
        "pair": (api_key, user),
        "db": db_session,
        "daemon": daemon,
        "clock": _clock(),
        "settings": _settings(),
        "idempotency_key": "stable-refill-2",
    }

    await refill_session_endpoint(
        body=RefillSessionRequest(observed_consumed_units=80),
        **arguments,
    )
    await db_session.commit()
    with pytest.raises(IdempotencyKeyReuse):
        await refill_session_endpoint(
            body=RefillSessionRequest(observed_consumed_units=81),
            **arguments,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_endpoint_recovers_lost_completed_payer_response_with_same_id(
    db_session: AsyncSession,
) -> None:
    daemon = LoseNextCompletedResponseDaemon()
    user_id, key_id, open_resp, _ = await _open_extensible_session(
        db_session,
        daemon=daemon,
    )
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    clock = _clock()
    settings = _settings()
    arguments = {
        "session_id": open_resp.session_id,
        "body": RefillSessionRequest(observed_consumed_units=80),
        "pair": (api_key, user),
        "db": db_session,
        "daemon": daemon,
        "clock": clock,
        "settings": settings,
        "idempotency_key": "lost-refill-response",
    }

    daemon.lose_next = True
    with pytest.raises(DaemonUnavailable):
        await refill_session_endpoint(**arguments)
    clock.advance(timedelta(seconds=settings.idempotency_inflight_timeout_seconds + 1))
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    arguments["pair"] = (api_key, user)
    recovered = await refill_session_endpoint(**arguments)
    await db_session.commit()

    assert recovered.request_id
    assert daemon.attempts == 3  # initial open + lost refill response + exact payer replay
    assert len(daemon._mint_replays) == 2
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
    ).all()
    assert len(payments) == 2
    assert payments[-1].mint_request_id == f"loc:{recovered.request_id}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_rejects_predecessor_and_remints_without_double_funding(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)
    rejected = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=80,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        request_id="rejected-refill",
    )

    replacement = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=80,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        request_id="rotation-successor",
        rebind_from=rejected.work_id,
        replaces_request_id=rejected.request_id,
    )

    assert replacement.work_id != rejected.work_id
    assert replacement.rebind_from == rejected.work_id
    assert daemon.reported_invalid_recipient_rands == [
        (rejected.work_id, "livepeer:vtuber-session", "vtuber-1080p30")
    ]
    session_row = await db_session.get(PaymentSession, open_resp.session_id)
    assert session_row is not None
    assert session_row.work_id == replacement.work_id
    assert session_row.rotation_generation == 1
    payments = (
        await db_session.scalars(
            select(Payment)
            .where(Payment.session_id == open_resp.session_id)
            .order_by(Payment.created_at)
        )
    ).all()
    assert [payment.status for payment in payments] == ["issued", "refused", "issued"]
    assert payments[1].refused_reason == "invalid_recipient_rand"
    assert payments[1].refunded_wei == payments[1].expected_value_wei
    assert (
        sum(
            int(payment.work_units_requested) for payment in payments if payment.status != "refused"
        )
        == 200
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_chain_closes_with_exactly_once_signed_accounting(
    db_session: AsyncSession,
) -> None:
    initial_balance = 10**12
    user_id, key_id, open_resp, daemon = await _open_extensible_session(
        db_session, balance_wei=initial_balance
    )
    rejected = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=80,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        request_id="rejected-refill",
    )
    replacement = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=80,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
        request_id="rotation-successor",
        rebind_from=rejected.work_id,
        replaces_request_id=rejected.request_id,
    )
    settlement = signed_session_settlement(
        gateway_session_id=str(open_resp.session_id),
        work_id=replacement.work_id,
        predecessor_work_id=rejected.work_id,
        rotation_generation=1,
        debited_units=150,
        generation_debited_units=50,
        billed_value_wei=150_000,
        generation_billed_value_wei=50_000,
        funded_value_wei=1_000_000,
        generation_funded_value_wei=100_000,
        amount_wei=1000,
        per_units=1,
        work_unit="session_second",
    )

    closed = await sessions_service.close_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        actual_units=150,
        outcome=None,
        settlement=settlement,
        clock=_clock(),
    )

    assert closed.billed_value_wei == 150_000
    assert closed.refund_wei == 850_000
    balance = await db_session.get(CreditBalance, user_id)
    assert balance is not None
    assert balance.amount_wei == Decimal(initial_balance - 150_000)
    with pytest.raises(SessionNotOpen):
        await sessions_service.close_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            actual_units=150,
            outcome=None,
            settlement=settlement,
            clock=_clock(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_requires_exact_issued_predecessor(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)

    with pytest.raises(InvalidSessionRequest, match="predecessor"):
        await sessions_service.refill_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            api_key_id=key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
            request_id="rotation-successor",
            rebind_from="00" * 32,
            replaces_request_id=open_resp.request_id,
        )
    assert daemon.reported_invalid_recipient_rands == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_requires_fresh_loc_and_payer_request_identity(
    db_session: AsyncSession,
) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)

    with pytest.raises(InvalidSessionRequest, match="fresh request identity"):
        await sessions_service.refill_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            api_key_id=key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
            request_id=open_resp.request_id,
            rebind_from=open_resp.work_id,
            replaces_request_id=open_resp.request_id,
        )
    assert daemon.reported_invalid_recipient_rands == []


@pytest.mark.unit
def test_rotation_request_requires_both_binding_fields() -> None:
    with pytest.raises(ValueError, match="must be supplied together"):
        RefillSessionRequest(rebind_from="ab" * 32)
    with pytest.raises(ValueError, match="must be supplied together"):
        RefillSessionRequest(replaces_request_id="prior-response")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_recovery_replays_after_lost_payer_response(
    db_session: AsyncSession,
) -> None:
    daemon = LoseNextCompletedResponseDaemon()
    user_id, key_id, open_resp, _ = await _open_extensible_session(db_session, daemon=daemon)
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    clock = _clock()
    settings = _settings()
    body = RefillSessionRequest(
        rebind_from=open_resp.work_id,
        replaces_request_id=open_resp.request_id,
    )
    arguments = {
        "session_id": open_resp.session_id,
        "body": body,
        "pair": (api_key, user),
        "db": db_session,
        "daemon": daemon,
        "clock": clock,
        "settings": settings,
        "idempotency_key": "rotation-after-lost-response",
    }

    daemon.lose_next = True
    with pytest.raises(DaemonUnavailable):
        await refill_session_endpoint(**arguments)
    clock.advance(timedelta(seconds=settings.idempotency_inflight_timeout_seconds + 1))
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    arguments["pair"] = (api_key, user)
    recovered = await refill_session_endpoint(**arguments)

    session_row = await db_session.get(PaymentSession, open_resp.session_id)
    assert recovered.work_id != open_resp.work_id
    assert session_row is not None
    assert session_row.rotation_generation == 1
    assert len(daemon.reported_invalid_recipient_rands) == 2
    assert daemon.attempts == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_endpoint_keeps_incomplete_payer_reservation_as_tombstone(
    db_session: AsyncSession,
) -> None:
    daemon = IncompleteNextReservationDaemon()
    user_id, key_id, open_resp, _ = await _open_extensible_session(
        db_session,
        daemon=daemon,
    )
    user = await db_session.get(User, user_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert user is not None
    assert api_key is not None
    arguments = {
        "session_id": open_resp.session_id,
        "body": RefillSessionRequest(observed_consumed_units=80),
        "pair": (api_key, user),
        "db": db_session,
        "daemon": daemon,
        "clock": _clock(),
        "settings": _settings(),
        "idempotency_key": "incomplete-refill",
    }

    daemon.fail_next = True
    for attempt in range(2):
        with pytest.raises(OpenClearinghouseError) as exc_info:
            await refill_session_endpoint(**arguments)
        assert exc_info.value.code == "IDEMPOTENCY_OUTCOME_UNKNOWN"
        if attempt == 0:
            user = await db_session.get(User, user_id)
            api_key = await db_session.get(ApiKey, key_id)
            assert user is not None
            assert api_key is not None
            arguments["pair"] = (api_key, user)

    assert daemon.attempts == 2  # initial open + one incomplete refill reservation
    payments = (
        await db_session.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
    ).all()
    assert len(payments) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_refill_requests_mint_and_account_once(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'refill-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    daemon = LoseNextCompletedResponseDaemon()
    async with maker() as seed_db:
        user_id, key_id, open_resp, _ = await _open_extensible_session(
            seed_db,
            daemon=daemon,
        )
        await seed_db.commit()

    async def invoke() -> str:
        async with maker() as db:
            user = await db.get(User, user_id)
            api_key = await db.get(ApiKey, key_id)
            assert user is not None
            assert api_key is not None
            try:
                await refill_session_endpoint(
                    session_id=open_resp.session_id,
                    body=RefillSessionRequest(observed_consumed_units=80),
                    pair=(api_key, user),
                    db=db,
                    daemon=daemon,
                    clock=_clock(),
                    settings=_settings(),
                    idempotency_key="concurrent-refill",
                )
                await db.commit()
            except OpenClearinghouseError as exc:
                return exc.code
            return "ok"

    results = await asyncio.gather(invoke(), invoke())
    assert "ok" in results
    assert set(results) <= {"ok", "IDEMPOTENCY_IN_PROGRESS"}
    assert daemon.attempts == 2  # initial open + one refill mint
    async with maker() as db:
        payments = (
            await db.scalars(select(Payment).where(Payment.session_id == open_resp.session_id))
        ).all()
        session_row = await db.get(PaymentSession, open_resp.session_id)
        assert len(payments) == 2
        assert session_row is not None
        assert session_row.refill_seq == 1
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_will_refuse_next_refill_when_approaching_session_cap(
    db_session: AsyncSession,
) -> None:
    """Refill until session_pct >= 95% and the next mint would push
    over → cap_status flips will_refuse_next_refill=True with
    winddown_reason='session_cap_imminent'."""
    user_id, key_id, open_resp, daemon = await _open_extensible_session(
        db_session, estimated=100, max_total=1000, price_wei=1000
    )

    # Initial mint already used 100_000 (10%). Refill 9 more times to
    # get to 100% — but that's a SessionCapReached. Refill to 90% (no
    # warning yet), then once more to 100% / refusal? Actually 0.95
    # threshold is on the most-recent cap_status; if we refill to
    # exactly 95% (after 9 refills + initial = 1_000_000) we hit cap.
    # Goal: get session_pct_used >= 0.95 with next_mint still fitting.
    # Initial = 0.1 + 8 refills = 0.9. After 8 refills, no warning.
    # After 9th refill, session_pct = 1.0 — but that'd be the FINAL
    # mint (no remaining for a 10th). Let me use max_total=1100 so
    # we have 1.1M cap and 11 mints fit. Skip this micro and verify
    # at-the-edge.
    # Recreate with larger headroom for clarity:
    pass

    # Refill 8 times (each +100_000); cumulative 9 x 100_000 = 900_000,
    # well below 1_000_000. Last cap_status should be 0.9.
    for _ in range(8):
        refill_resp = await sessions_service.refill_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=user_id,
            api_key_id=key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )
    # After 8 refills + 1 initial mint = 9 x 100_000 = 900_000 (90%)
    assert refill_resp.cap_status.session_pct_used == pytest.approx(0.9)
    assert refill_resp.cap_status.will_refuse_next_refill is False

    # 9th refill → 1_000_000 (100%) — but the projected NEXT after this
    # would be 1_100_000 which exceeds funded=1_000_000, so this refill
    # response should flip will_refuse_next_refill=True.
    refill_resp = await sessions_service.refill_session(
        db_session,
        session_id=open_resp.session_id,
        user_id=user_id,
        api_key_id=key_id,
        observed_consumed_units=None,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    assert refill_resp.cap_status.session_pct_used == pytest.approx(1.0)
    assert refill_resp.cap_status.will_refuse_next_refill is True
    assert refill_resp.cap_status.winddown_reason == "session_cap_imminent"


# ---- error paths ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_rejects_unknown_session(db_session: AsyncSession) -> None:
    user_id, key_id = await _seed(db_session)
    daemon = MockPaymentDaemonClient()
    with pytest.raises(SessionNotFound):
        await sessions_service.refill_session(
            db_session,
            session_id=uuid.uuid4(),
            user_id=user_id,
            api_key_id=key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_rejects_session_owned_by_different_user(
    db_session: AsyncSession,
) -> None:
    _, _, open_resp, daemon = await _open_extensible_session(db_session)
    # Create a SECOND user; try to refill the first user's session as them.
    other_user_id, other_key_id = await _seed(db_session, balance_wei=10**18)
    with pytest.raises(SessionNotFound):
        await sessions_service.refill_session(
            db_session,
            session_id=open_resp.session_id,
            user_id=other_user_id,
            api_key_id=other_key_id,
            observed_consumed_units=None,
            daemon=daemon,
            clock=_clock(),
            settings=_settings(),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_rejects_closed_session(db_session: AsyncSession) -> None:
    user_id, key_id, open_resp, daemon = await _open_extensible_session(db_session)
    # Close the session manually.
    await sessions_service.transition_state(
        db_session,
        open_resp.session_id,
        from_state=SESSION_STATE_OPEN,
        to_state=SESSION_STATE_CLOSED,
        clock=_clock(),
    )
    with pytest.raises(SessionNotOpen):
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_rejects_bounded_declaration(db_session: AsyncSession) -> None:
    """session.refill=bounded refuses top-up independently of runtime shape."""
    user_id, key_id = await _seed(db_session)
    registry = MockRegistryClient(routes=[_route(refill="bounded")])
    daemon = MockPaymentDaemonClient()
    open_resp = await sessions_service.open_session(
        db_session,
        user_id=user_id,
        api_key_id=key_id,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        estimated_runway_units=100,
        max_total_units=1000,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=_clock(),
        settings=_settings(),
    )
    with pytest.raises(RefillNotSupported):
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refill_rejects_when_session_cap_exhausted(
    db_session: AsyncSession,
) -> None:
    """Open small session; refill to exhaustion; final refill refused
    with cap_reached / which=session."""
    user_id, key_id, open_resp, daemon = await _open_extensible_session(
        db_session, estimated=100, max_total=200, price_wei=1000
    )
    # Initial mint used 100_000 of 200_000 (50%). One refill takes us
    # to 100% (200_000 / 200_000). A second refill should be refused.
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
    with pytest.raises(SessionCapReached) as exc_info:
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
    assert exc_info.value.details["which"] == "session"
    assert exc_info.value.status_code == 402
