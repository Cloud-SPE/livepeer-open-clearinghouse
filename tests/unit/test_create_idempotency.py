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
        settlement_domain_id="0x" + "aa" * 32,
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
            workload_request_digest="44" * 32,
            caller_public_key="02" + "55" * 32,
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
@pytest.mark.parametrize(
    ("operation", "released"),
    [("jobs.create", False), ("sessions.prepare", False), ("sessions.prepare", True)],
)
async def test_concurrent_stale_recovery_has_one_winner_and_stable_id(
    tmp_path: Path, operation: str, released: bool
) -> None:
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
            operation=operation,
            idempotency_key="stale-job",
            request_fingerprint=_fingerprint(),
            clock=clock,
            inflight_timeout_seconds=60,
        )
        if released:
            await service.release_failed_session_preparation(
                seed_session, user_id=user_id, idempotency_key="stale-job", claim=first
            )
    if not released:
        clock.advance(timedelta(seconds=61))

    async def reclaim() -> tuple[str, str | None]:
        async with maker() as session:
            try:
                claim = await service.claim_create_request(
                    session,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    operation=operation,
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
    if operation == "sessions.prepare":
        async with maker() as session:
            await service.release_failed_session_preparation(
                session, user_id=user_id, idempotency_key="stale-job", claim=first
            )
            row = await session.get(PaymentIdempotencyKey, (user_id, operation, "stale-job"))
            assert row.status == "in_flight"
            assert row.expires_at.replace(tzinfo=UTC) > first.lease_expires_at
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


@pytest.mark.unit
@pytest.mark.parametrize("elapsed", [0, 301, -1])
async def test_failed_preparation_retry_preserves_identity_and_fences_old_attempt(
    db: AsyncSession, elapsed: int
) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 9, 23, tzinfo=UTC))
    arguments = dict(
        user_id=user_id,
        api_key_id=api_key_id,
        operation="sessions.prepare",
        idempotency_key="prepare",
        request_fingerprint="a" * 64,
        clock=clock,
        inflight_timeout_seconds=300,
    )
    first = await service.claim_create_request(db, **arguments)
    await service.release_failed_session_preparation(
        db, user_id=user_id, idempotency_key="prepare", claim=first
    )
    assert not db.in_transaction()
    clock.advance(timedelta(seconds=elapsed))
    second = await service.claim_create_request(db, **arguments)
    assert second.broker_request_id == first.broker_request_id
    assert second.lease_expires_at > first.lease_expires_at
    await service.release_failed_session_preparation(
        db, user_id=user_id, idempotency_key="prepare", claim=first
    )
    with pytest.raises(IdempotencyInProgress):
        await service.claim_create_request(db, **arguments)
    with pytest.raises(IdempotencyKeyReuse):
        await service.claim_create_request(db, **(arguments | {"request_fingerprint": "b" * 64}))
    await service.complete_create_request(
        db,
        user_id=user_id,
        operation="sessions.prepare",
        idempotency_key="prepare",
        http_status=201,
        response_payload={"prepared": "stable"},
        clock=clock,
        retention_seconds=3600,
    )
    await service.release_failed_session_preparation(
        db, user_id=user_id, idempotency_key="prepare", claim=second
    )
    replay = await service.claim_create_request(db, **arguments)
    assert replay.replay_payload == {"prepared": "stable"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("operation", "status"),
    [
        ("sessions.create", "in_flight"),
        ("sessions.refill:s", "in_flight"),
        ("jobs.create", "in_flight"),
        ("sessions.prepare", "outcome_unknown"),
        ("sessions.prepare", "completed"),
        ("sessions.prepare", "failed"),
    ],
)
async def test_preparation_release_never_changes_paid_or_terminal_claims(
    db: AsyncSession, operation: str, status: str
) -> None:
    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 9, 23, tzinfo=UTC))
    claim = await service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation=operation,
        idempotency_key="same",
        request_fingerprint="a" * 64,
        clock=clock,
        inflight_timeout_seconds=300,
    )
    row = await db.get(PaymentIdempotencyKey, (user_id, operation, "same"))
    row.status = status
    await db.commit()
    await service.release_failed_session_preparation(
        db, user_id=user_id, idempotency_key="same", claim=claim
    )
    await db.refresh(row)
    assert row.status == status
    assert row.expires_at.replace(tzinfo=UTC) == claim.lease_expires_at


@pytest.mark.unit
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("failure", ["NOT_FOUND", "DEADLINE_EXCEEDED"])
async def test_prepare_http_failure_retries_immediately_and_replays(
    db: AsyncSession, bound: bool, failure: str
) -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import grpc
    import httpx
    from fastapi import FastAPI
    from livepeer.registry.v1 import resolver_pb2

    from livepeer_open_clearinghouse import dependencies
    from livepeer_open_clearinghouse.domains.sessions import runtime
    from livepeer_open_clearinghouse.errors import register_handlers
    from livepeer_open_clearinghouse.providers.registry_daemon import (
        CachingRegistryClient,
        GrpcRegistryClient,
    )
    from livepeer_open_clearinghouse.settings import Settings

    user_id, api_key_id = await _identity(db)
    clock = FrozenClock(datetime(2026, 9, 23, tzinfo=UTC))
    proto = resolver_pb2.SelectedRoute(
        worker_url="https://broker.example",
        eth_address="0x" + "11" * 20,
        capability="video:transcode.live",
        offering="gateway-ingest",
        price_per_work_unit_wei="10",
        work_unit="second",
        units_per_price=1,
        quote_id="live",
        quote_version=1,
        constraint_fingerprint=b"a" * 32,
        route_fingerprint=b"b" * 32,
        settlement_domain_id="0x" + "cc" * 32,
        protocol="paid-session/v1",
        extra_json=json.dumps(
            {
                "session": {
                    "descriptor_schema": "rtmp-hls/v1",
                    "metering": "runner-reported",
                    "attachment": "external",
                    "refill": "bounded",
                }
            }
        ).encode(),
    )
    error = grpc.aio.AioRpcError(
        getattr(grpc.StatusCode, failure), (), (), "private upstream credentials"
    )
    success = (
        resolver_pb2.SelectManyResult(routes=[proto])
        if bound
        else resolver_pb2.SelectResult(route=proto)
    )
    calls = AsyncMock(side_effect=[error, error, success])
    inner = GrpcRegistryClient("/unused")
    inner._ensure_stub = AsyncMock(
        return_value=SimpleNamespace(**{"SelectMany" if bound else "Select": calls})
    )
    registry = CachingRegistryClient(inner, ttl_seconds=60)
    settings = Settings(_env_file=None)
    app = FastAPI()
    register_handlers(app)
    app.include_router(runtime.router)

    async def pair():
        # Real ORM instances catch attribute reloads after rollback.
        return await db.get(ApiKey, api_key_id), await db.get(User, user_id)

    app.dependency_overrides.update(
        {
            dependencies.get_current_api_key_and_user: pair,
            dependencies.get_session: lambda: db,
            dependencies.get_clock: lambda: clock,
            dependencies.get_registry: lambda: registry,
            dependencies.get_settings_dep: lambda: settings,
        }
    )
    body = {
        "capability": "video:transcode.live",
        "offering": "gateway-ingest",
        "descriptor_schema": "rtmp-hls/v1",
    }
    if bound:
        body["route_binding"] = {
            "quote_id": "live",
            "quote_version": 1,
            "constraint_fingerprint": (b"a" * 32).hex(),
            "route_fingerprint": (b"b" * 32).hex(),
            "settlement_domain_id": "0x" + "cc" * 32,
        }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        identities = []
        for _ in range(2):
            response = await client.post(
                "/v1/sessions/prepare", json=body, headers={"Idempotency-Key": "same"}
            )
            assert response.status_code == (
                503 if failure == "DEADLINE_EXCEEDED" else (409 if bound else 404)
            )
            assert "private" not in response.text
            row = await db.get(PaymentIdempotencyKey, (user_id, "sessions.prepare", "same"))
            identities.append(row.broker_request_id)
            assert row.status == "expired"
            db.expunge(row)
        assert identities[0] == identities[1]
        response = await client.post(
            "/v1/sessions/prepare", json=body, headers={"Idempotency-Key": "same"}
        )
        assert response.status_code == 201
        replay = await client.post(
            "/v1/sessions/prepare", json=body, headers={"Idempotency-Key": "same"}
        )
        assert replay.json() == response.json()
        assert calls.await_count == 3
        changed = await client.post(
            "/v1/sessions/prepare",
            json=body | {"offering": "changed"},
            headers={"Idempotency-Key": "same"},
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSE"
