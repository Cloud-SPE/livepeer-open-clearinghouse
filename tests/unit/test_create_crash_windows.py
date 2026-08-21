"""Composed LOC/payer idempotency tests across create crash windows."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance, CreditLedger
from livepeer_open_clearinghouse.domains.jobs.runtime import open_job_endpoint
from livepeer_open_clearinghouse.domains.jobs.types import CreateJobRequest
from livepeer_open_clearinghouse.domains.notifications import repo as _notifications  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.domains.payments.repo import Payment, PaymentIdempotencyKey
from livepeer_open_clearinghouse.domains.sessions import repo as _sessions  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.sessions.runtime import open_session_endpoint
from livepeer_open_clearinghouse.domains.sessions.types import CreateSessionRequest
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    IdempotencyInProgress,
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
)
from livepeer_open_clearinghouse.settings import Settings

INITIAL_BALANCE = Decimal(1_000_000)
ENCUMBERED = Decimal(1_000)


@dataclass(frozen=True, slots=True)
class CreateCase:
    kind: Literal["job", "session"]
    operation: str
    body: CreateJobRequest | CreateSessionRequest
    route: SelectedRoute


def _route(protocol: Literal["paid-job/v1", "paid-session/v1"]) -> SelectedRoute:
    extra: dict[str, Any]
    if protocol == "paid-job/v1":
        extra = {"job": {"transports": ["unary"]}}
    else:
        extra = {
            "session": {
                "descriptor_schema": "test-runtime/v1",
                "metering": "runner-reported",
                "refill": "extensible",
            }
        }
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="test:paid-create",
        offering="default",
        price_per_work_unit_wei=Decimal(100),
        work_unit="token",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol=protocol,
        extra=extra,
    )


CASES = (
    CreateCase(
        kind="job",
        operation="jobs.create",
        body=CreateJobRequest(
            capability="test:paid-create",
            offering="default",
            transport="unary",
            estimated_units=5,
            max_total_units=10,
        ),
        route=_route("paid-job/v1"),
    ),
    CreateCase(
        kind="session",
        operation="sessions.create",
        body=CreateSessionRequest(
            capability="test:paid-create",
            offering="default",
            descriptor_schema="test-runtime/v1",
            session_params={"test": "value"},
            estimated_runway_units=5,
            max_total_units=10,
        ),
        route=_route("paid-session/v1"),
    ),
)


@pytest_asyncio.fixture()
async def database(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crash-windows.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4().hex}@example.com",
            email_verified_at=datetime.now(UTC),
            password_hash="x",
        )
        key = ApiKey(
            id=uuid.uuid4(),
            user_id=user.id,
            prefix=f"loc_{uuid.uuid4().hex[:12]}",
            hash="hash",
            label="test",
        )
        db.add_all([user, key])
        await db.flush()
        db.add(CreditBalance(user_id=user.id, amount_wei=INITIAL_BALANCE))
        await db.commit()
        user_id, key_id = user.id, key.id
    yield maker, user_id, key_id
    await engine.dispose()


class CountingDaemon(MockPaymentDaemonClient):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    @property
    def unique_mints(self) -> int:
        return len(self._mint_replays)

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        self.attempts += 1
        return await super().create_payment(request)


class LoseFirstCompletedResponseDaemon(CountingDaemon):
    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        response = await super().create_payment(request)
        if self.attempts == 1:
            raise PaymentDaemonError("response lost after durable payer completion")
        return response


class IncompleteReservationDaemon(CountingDaemon):
    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        self.attempts += 1
        raise MintOutcomeUnknown("mint_request_id was reserved but never completed; use a new id")


def _settings() -> Settings:
    return Settings(
        admin_bootstrap_token="x",
        session_signing_secret="x",
        database_url="sqlite+aiosqlite:///:memory:",
        idempotency_inflight_timeout_seconds=5,
        idempotency_retention_seconds=60,
    )


async def _invoke(
    case: CreateCase,
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    daemon: MockPaymentDaemonClient,
    clock: FrozenClock,
    idempotency_key: str,
) -> Any:
    user = await db.get(User, user_id)
    key = await db.get(ApiKey, key_id)
    assert user is not None
    assert key is not None
    common = {
        "pair": (key, user),
        "db": db,
        "registry": MockRegistryClient(routes=[case.route]),
        "daemon": daemon,
        "clock": clock,
        "settings": _settings(),
        "idempotency_key": idempotency_key,
        "sdk_identity": "fault-test/1",
    }
    if case.kind == "job":
        assert isinstance(case.body, CreateJobRequest)
        return await open_job_endpoint(body=case.body, **common)
    assert isinstance(case.body, CreateSessionRequest)
    return await open_session_endpoint(body=case.body, **common)


async def _assert_one_committed_create(
    maker: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    request_id: str,
) -> None:
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Payment)) == 1
        assert await db.scalar(select(func.count()).select_from(PaymentSession)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CreditLedger)
                .where(CreditLedger.reason == "session_encumbrance")
            )
            == 1
        )
        balance = await db.get(CreditBalance, user_id)
        assert balance is not None
        assert balance.amount_wei == INITIAL_BALANCE - ENCUMBERED
        payment = await db.scalar(select(Payment))
        assert payment is not None
        assert payment.mint_request_id == f"loc:{request_id}"
        claim = await db.get(
            PaymentIdempotencyKey,
            {
                "user_id": user_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
            },
        )
        assert claim is not None
        assert claim.status == "completed"
        assert claim.broker_request_id == request_id


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_claim_commit_failure_happens_before_any_payer_side_effect(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    daemon = CountingDaemon()

    async with maker() as db:

        async def fail_commit() -> None:
            raise RuntimeError("injected claim commit failure")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected claim commit failure"):
            await _invoke(
                case,
                db,
                user_id=user_id,
                key_id=key_id,
                daemon=daemon,
                clock=clock,
                idempotency_key=f"{case.kind}-claim-commit-failure",
            )
        await db.rollback()

    assert daemon.attempts == 0
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(PaymentIdempotencyKey)) == 0
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert await db.scalar(select(func.count()).select_from(PaymentSession)) == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_crash_after_claim_before_daemon_recovers_same_request_id(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    key = f"{case.kind}-after-claim"
    async with maker() as db:
        claim = await payments_service.claim_create_request(
            db,
            user_id=user_id,
            api_key_id=key_id,
            operation=case.operation,
            idempotency_key=key,
            request_fingerprint=payments_service.create_request_fingerprint(
                operation=case.operation,
                payload=case.body.model_dump(mode="json"),
            ),
            clock=clock,
            inflight_timeout_seconds=5,
        )

    clock.advance(timedelta(seconds=6))
    daemon = CountingDaemon()
    async with maker() as restarted_db:
        response = await _invoke(
            case,
            restarted_db,
            user_id=user_id,
            key_id=key_id,
            daemon=daemon,
            clock=clock,
            idempotency_key=key,
        )
        await restarted_db.commit()

    assert response.request_id == claim.broker_request_id
    assert daemon.attempts == 1
    assert daemon.unique_mints == 1
    await _assert_one_committed_create(
        maker,
        user_id=user_id,
        operation=case.operation,
        idempotency_key=key,
        request_id=claim.broker_request_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_stale_loc_claim_recovers_completed_payer_result_once(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    daemon = LoseFirstCompletedResponseDaemon()
    key = f"{case.kind}-lost-payer-response"

    async with maker() as db:
        with pytest.raises(DaemonUnavailable):
            await _invoke(
                case,
                db,
                user_id=user_id,
                key_id=key_id,
                daemon=daemon,
                clock=clock,
                idempotency_key=key,
            )

    clock.advance(timedelta(seconds=6))
    async with maker() as db:
        response = await _invoke(
            case,
            db,
            user_id=user_id,
            key_id=key_id,
            daemon=daemon,
            clock=clock,
            idempotency_key=key,
        )
        await db.commit()

    assert daemon.attempts == 2
    assert daemon.unique_mints == 1
    await _assert_one_committed_create(
        maker,
        user_id=user_id,
        operation=case.operation,
        idempotency_key=key,
        request_id=response.request_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_incomplete_payer_reservation_is_terminal_outcome_unknown(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    daemon = IncompleteReservationDaemon()
    key = f"{case.kind}-incomplete-payer"

    for _ in range(2):
        async with maker() as db:
            with pytest.raises(OpenClearinghouseError) as exc_info:
                await _invoke(
                    case,
                    db,
                    user_id=user_id,
                    key_id=key_id,
                    daemon=daemon,
                    clock=clock,
                    idempotency_key=key,
                )
            assert exc_info.value.code == "IDEMPOTENCY_OUTCOME_UNKNOWN"

    clock.advance(timedelta(seconds=61))
    async with maker() as db:
        assert await payments_service.expire_stale_idempotency_keys(db, clock=clock) == 0
        await db.commit()
        claim = await db.get(
            PaymentIdempotencyKey,
            {
                "user_id": user_id,
                "operation": case.operation,
                "idempotency_key": key,
            },
        )
        assert claim is not None
        assert claim.status == "outcome_unknown"

    async with maker() as db:
        with pytest.raises(OpenClearinghouseError) as exc_info:
            await _invoke(
                case,
                db,
                user_id=user_id,
                key_id=key_id,
                daemon=daemon,
                clock=clock,
                idempotency_key=key,
            )
        assert exc_info.value.code == "IDEMPOTENCY_OUTCOME_UNKNOWN"

    assert daemon.attempts == 1
    assert daemon.unique_mints == 0
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert await db.scalar(select(func.count()).select_from(PaymentSession)) == 0
        assert await db.scalar(select(func.count()).select_from(CreditLedger)) == 0
        balance = await db.get(CreditBalance, user_id)
        assert balance is not None
        assert balance.amount_wei == INITIAL_BALANCE


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_committed_result_replays_after_lost_http_response_and_restart(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    daemon = CountingDaemon()
    key = f"{case.kind}-lost-http-response"

    async with maker() as db:
        first = await _invoke(
            case,
            db,
            user_id=user_id,
            key_id=key_id,
            daemon=daemon,
            clock=clock,
            idempotency_key=key,
        )
        await db.commit()

    async with maker() as restarted_db:
        replay = await _invoke(
            case,
            restarted_db,
            user_id=user_id,
            key_id=key_id,
            daemon=daemon,
            clock=clock,
            idempotency_key=key,
        )

    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    assert daemon.attempts == 1
    assert daemon.unique_mints == 1
    await _assert_one_committed_create(
        maker,
        user_id=user_id,
        operation=case.operation,
        idempotency_key=key,
        request_id=first.request_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.kind)
async def test_concurrent_duplicate_creates_have_one_mint_and_one_mutation(
    database: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID],
    case: CreateCase,
) -> None:
    maker, user_id, key_id = database
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    daemon = CountingDaemon()
    key = f"{case.kind}-concurrent"

    async def invoke() -> Any:
        async with maker() as db:
            try:
                response = await _invoke(
                    case,
                    db,
                    user_id=user_id,
                    key_id=key_id,
                    daemon=daemon,
                    clock=clock,
                    idempotency_key=key,
                )
            except IdempotencyInProgress as exc:
                return exc
            await db.commit()
            return response

    results = await asyncio.gather(invoke(), invoke())
    responses = [result for result in results if not isinstance(result, OpenClearinghouseError)]
    assert responses
    assert all(result.request_id == responses[0].request_id for result in responses)
    assert daemon.attempts == 1
    assert daemon.unique_mints == 1
    await _assert_one_committed_create(
        maker,
        user_id=user_id,
        operation=case.operation,
        idempotency_key=key,
        request_id=responses[0].request_id,
    )
