"""Durability and replay tests for job/session create idempotency."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import every repo so all foreign-key targets are present in Base.metadata.
from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.jobs.types import CreateJobRequest
from livepeer_open_clearinghouse.domains.notifications import repo as _notifications  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import service
from livepeer_open_clearinghouse.domains.payments.repo import PaymentIdempotencyKey
from livepeer_open_clearinghouse.domains.sessions import repo as _sessions  # noqa: F401
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.errors import (
    IdempotencyInProgress,
    IdempotencyKeyReuse,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.registry_daemon import RouteBinding


@pytest_asyncio.fixture()
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _identity(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        email_verified_at=datetime.now(UTC),
        password_hash="x",
    )
    api_key = ApiKey(
        id=uuid.uuid4(),
        user_id=user.id,
        prefix=f"loc_{uuid.uuid4().hex[:12]}",
        hash="hash",
        label="test",
    )
    db.add_all([user, api_key])
    await db.commit()
    return user.id, api_key.id


def _fingerprint(units: int = 10) -> str:
    return service.create_request_fingerprint(
        operation="jobs.create",
        payload={"capability": "llm", "offering": "chat", "units": units},
    )


@pytest.mark.unit
def test_route_binding_is_part_of_open_idempotency_fingerprint() -> None:
    first = RouteBinding(
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint="00" * 32,
        route_fingerprint="11" * 32,
    )
    changed = first.model_copy(update={"route_fingerprint": "22" * 32})

    def fingerprint(binding: RouteBinding) -> str:
        body = CreateJobRequest(
            capability="livepeer:transcoder/h264",
            offering="h264-1080p",
            transport="unary",
            estimated_units=100,
            max_total_units=100,
            route_binding=binding,
        )
        return service.create_request_fingerprint(
            operation="jobs.create",
            payload=body.model_dump(mode="json"),
        )

    assert fingerprint(first) == fingerprint(first)
    assert fingerprint(first) != fingerprint(changed)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completed_claim_replays_stable_response(db: AsyncSession) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    claim = await service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation="jobs.create",
        idempotency_key="job-123",
        request_fingerprint=_fingerprint(),
        clock=clock,
        inflight_timeout_seconds=60,
    )

    await service.complete_create_request(
        db,
        user_id=user_id,
        operation="jobs.create",
        idempotency_key="job-123",
        http_status=201,
        response_payload={"request_id": claim.broker_request_id, "job_id": "stable"},
        clock=clock,
        retention_seconds=3600,
    )
    assert not db.in_transaction(), "completion must be durable before HTTP returns"

    replay = await service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation="jobs.create",
        idempotency_key="job-123",
        request_fingerprint=_fingerprint(),
        clock=clock,
        inflight_timeout_seconds=60,
    )
    assert replay.is_replay
    assert replay.broker_request_id == claim.broker_request_id
    assert replay.replay_payload == {
        "request_id": claim.broker_request_id,
        "job_id": "stable",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_claim_rejects_inflight_and_changed_content(db: AsyncSession) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    arguments = {
        "user_id": user_id,
        "api_key_id": api_key_id,
        "operation": "jobs.create",
        "idempotency_key": "job-123",
        "clock": clock,
        "inflight_timeout_seconds": 60,
    }
    await service.claim_create_request(db, request_fingerprint=_fingerprint(), **arguments)

    with pytest.raises(IdempotencyInProgress):
        await service.claim_create_request(db, request_fingerprint=_fingerprint(), **arguments)
    with pytest.raises(IdempotencyKeyReuse):
        await service.claim_create_request(db, request_fingerprint=_fingerprint(11), **arguments)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_claims_create_one_durable_winner(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'idempotency.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as seed_session:
        user_id, api_key_id = await _identity(seed_session)
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))

    async def claim() -> str:
        async with maker() as session:
            try:
                await service.claim_create_request(
                    session,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    operation="jobs.create",
                    idempotency_key="concurrent-job",
                    request_fingerprint=_fingerprint(),
                    clock=clock,
                    inflight_timeout_seconds=60,
                )
            except IdempotencyInProgress:
                return "in_progress"
            return "claimed"

    results = await asyncio.gather(claim(), claim())
    assert sorted(results) == ["claimed", "in_progress"]
    async with maker() as inspection_session:
        count = await inspection_session.scalar(
            select(func.count()).select_from(PaymentIdempotencyKey)
        )
    assert count == 1
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_stale_recovery_has_one_winner_and_stable_id(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as seed_session:
        user_id, api_key_id = await _identity(seed_session)
        clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
        first = await service.claim_create_request(
            seed_session,
            user_id=user_id,
            api_key_id=api_key_id,
            operation="jobs.create",
            idempotency_key="stale-job",
            request_fingerprint=_fingerprint(),
            clock=clock,
            inflight_timeout_seconds=60,
        )
    clock.advance(timedelta(seconds=61))

    async def reclaim() -> tuple[str, str | None]:
        async with maker() as session:
            try:
                claim = await service.claim_create_request(
                    session,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    operation="jobs.create",
                    idempotency_key="stale-job",
                    request_fingerprint=_fingerprint(),
                    clock=clock,
                    inflight_timeout_seconds=60,
                )
            except IdempotencyInProgress:
                return "in_progress", None
            return "claimed", claim.broker_request_id

    results = await asyncio.gather(reclaim(), reclaim())
    assert sorted(result[0] for result in results) == ["claimed", "in_progress"]
    winner_id = next(result[1] for result in results if result[0] == "claimed")
    assert winner_id == first.broker_request_id
    await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_claim_is_reclaimed_with_stable_request_id(db: AsyncSession) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    arguments = {
        "user_id": user_id,
        "api_key_id": api_key_id,
        "operation": "sessions.create",
        "idempotency_key": "session-123",
        "request_fingerprint": "a" * 64,
        "clock": clock,
        "inflight_timeout_seconds": 60,
    }
    first = await service.claim_create_request(db, **arguments)
    clock.advance(timedelta(seconds=61))
    assert await service.expire_stale_idempotency_keys(db, clock=clock) == 1
    await db.commit()

    recovered = await service.claim_create_request(db, **arguments)
    assert recovered.broker_request_id == first.broker_request_id
    assert not recovered.is_replay

    with pytest.raises(IdempotencyInProgress):
        await service.claim_create_request(db, **arguments)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_claim_replays_error_envelope(db: AsyncSession) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 8, 20, tzinfo=UTC))
    await service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation="sessions.create",
        idempotency_key="session-123",
        request_fingerprint="a" * 64,
        clock=clock,
        inflight_timeout_seconds=60,
    )
    error = {
        "error": {
            "code": "NO_ROUTE_AVAILABLE",
            "message": "No matching route",
            "details": {},
        }
    }
    await service.fail_create_request(
        db,
        user_id=user_id,
        operation="sessions.create",
        idempotency_key="session-123",
        http_status=404,
        response_payload=error,
        clock=clock,
        retention_seconds=3600,
    )

    replay = await service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation="sessions.create",
        idempotency_key="session-123",
        request_fingerprint="a" * 64,
        clock=clock,
        inflight_timeout_seconds=60,
    )
    assert replay.replay_status == 404
    assert replay.replay_payload == error
