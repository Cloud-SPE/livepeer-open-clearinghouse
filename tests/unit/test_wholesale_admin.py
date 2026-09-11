"""Operator visibility for customer-neutral wholesale exposure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.admin import service
from livepeer_open_clearinghouse.domains.wholesale.repo import (
    WholesaleAccount,
    WholesaleExposureBudget,
    WholesaleFunding,
)
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.settings import Settings

NOW = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
pytestmark = pytest.mark.unit


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        wholesale_chain_id=42161,
        wholesale_target_available_wei=1000,
        wholesale_replenish_below_wei=250,
        wholesale_max_available_per_payee_wei=2000,
        wholesale_max_aggregate_available_wei=5000,
        wholesale_max_single_funding_wei=1000,
    )


@pytest.mark.asyncio
async def test_wholesale_overview_flags_stale_account_and_stalled_funding(
    db_session: AsyncSession,
) -> None:
    account = WholesaleAccount(
        chain_id=42161,
        payer_eth_address="0x" + "11" * 20,
        payee_eth_address="0x" + "22" * 20,
        denomination="wei",
        protocol_version="wholesale-account/1.1.0-draft",
        broker_url="https://broker.example",
        credited_value_wei=Decimal(1000),
        reserved_value_wei=Decimal(200),
        debited_value_wei=Decimal(300),
        available_value_wei=Decimal(500),
        remote_version=7,
        observed_at=NOW - timedelta(minutes=10),
    )
    db_session.add_all(
        [account, WholesaleExposureBudget(scope="global", projected_available_wei=Decimal(800))]
    )
    await db_session.flush()
    db_session.add(
        WholesaleFunding(
            id=uuid.uuid4(),
            account_id=account.id,
            mint_request_id="loc-account:test",
            correlation_id="opaque-engagement",
            target_available_wei=Decimal(1000),
            observed_available_wei=Decimal(100),
            requested_shortfall_wei=Decimal(900),
            route_snapshot={},
            payment_bytes=b"opaque",
            minted_expected_value_wei=Decimal(900),
            credited_value_wei=None,
            work_id="funding-work",
            account_version=None,
            status="minted",
            acknowledged_at=None,
            created_at=NOW - timedelta(minutes=6),
        )
    )
    await db_session.commit()

    result = await service.wholesale_overview(
        db_session, clock=FrozenClock(NOW), settings=_settings()
    )

    assert result.projected_available_wei == Decimal(800)
    assert result.observed_available_wei == Decimal(500)
    assert result.aggregate_headroom_wei == Decimal(4200)
    assert result.stale_accounts == 1
    assert result.pending_fundings == 1
    assert result.accounts[0].stale is True
    assert result.fundings[0].needs_attention is True
    assert result.fundings[0].has_replayable_payment is True
    payload = result.model_dump(mode="json")
    assert payload["projected_available_wei"] == "800"
    assert "payment_bytes" not in payload["fundings"][0]
    assert "user_id" not in payload["accounts"][0]


def test_wholesale_admin_endpoint_requires_operator(client: TestClient) -> None:
    response = client.get("/v1/admin/wholesale")
    assert response.status_code == 401
