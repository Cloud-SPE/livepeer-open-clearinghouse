"""Tests for ingest-time enrichment: GeoIP provider, ingest_node_id
resolution, end-to-end column population."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.telemetry import service as telemetry_service
from livepeer_open_clearinghouse.domains.telemetry.enrichment import (
    Enrichment,
    EnrichmentContext,
    NoopGeoIPProvider,
    enrich,
    resolve_ingest_node_id,
)
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.telemetry.types import IngestEventIn
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture()
async def user_and_key(session: AsyncSession) -> tuple[User, ApiKey]:
    user = User(email="t@example.com")
    session.add(user)
    await session.flush()
    api_key = ApiKey(
        user_id=user.id,
        prefix="pymth_live_aaaa",
        hash="h",
        label="t",
    )
    session.add(api_key)
    await session.flush()
    return user, api_key


class _StubGeoIP:
    """Deterministic GeoIP for tests — IP→region static map."""

    def __init__(self, mapping: dict[str | None, str | None]) -> None:
        self._mapping = mapping

    def lookup(self, source_ip: str | None) -> str | None:
        return self._mapping.get(source_ip)


@pytest.mark.unit
class TestResolveIngestNodeId:
    def test_configured_wins(self) -> None:
        assert resolve_ingest_node_id("ingest-01") == "ingest-01"

    def test_hostname_fallback(self) -> None:
        # Just confirm it returns a non-empty string (whatever the
        # current host is named). Don't assert the value.
        result = resolve_ingest_node_id(None)
        assert isinstance(result, str)
        assert result != ""


@pytest.mark.unit
class TestNoopGeoIPProvider:
    def test_returns_none_for_any_ip(self) -> None:
        provider = NoopGeoIPProvider()
        assert provider.lookup(None) is None
        assert provider.lookup("8.8.8.8") is None
        assert provider.lookup("::1") is None


@pytest.mark.unit
def test_enrich_uses_geoip_provider() -> None:
    ctx = EnrichmentContext(
        source_ip="203.0.113.7",
        ingest_node_id="ingest-test",
        geoip=_StubGeoIP({"203.0.113.7": "us-east"}),
    )
    e = enrich(ctx)
    assert e.geo_region == "us-east"
    assert e.ingest_node_id == "ingest-test"
    # Reserved slots stay None until later PRs populate them.
    assert e.account_tier is None
    assert e.broker_operator_id is None


@pytest.mark.unit
def test_enrich_with_unknown_ip_returns_none_region() -> None:
    ctx = EnrichmentContext(
        source_ip="10.0.0.1",
        ingest_node_id="ingest-test",
        geoip=_StubGeoIP({}),  # IP not in map
    )
    e = enrich(ctx)
    assert e.geo_region is None
    assert e.ingest_node_id == "ingest-test"


@pytest.mark.unit
async def test_ingest_batch_stamps_enrichment_columns(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    clock = FrozenClock(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))
    events = [
        IngestEventIn(
            event_type="sdk.init",
            event_schema_version=1,
            payload={"lang": "py"},
        )
    ]
    enrichment = Enrichment(
        geo_region="us-west",
        account_tier="enterprise",
        broker_operator_id="0xabcdef0123456789abcdef0123456789abcdef01",
        ingest_node_id="ingest-02",
    )
    accepted, _ = await telemetry_service.ingest_batch(
        session,
        api_key_id=api_key.id,
        user_id=user.id,
        events=events,
        clock=clock,
        enrichment=enrichment,
    )
    assert accepted == 1
    row = (await session.scalars(select(TelemetryEvent))).one()
    assert row.geo_region == "us-west"
    assert row.account_tier == "enterprise"
    assert row.broker_operator_id == "0xabcdef0123456789abcdef0123456789abcdef01"
    assert row.ingest_node_id == "ingest-02"


@pytest.mark.unit
async def test_ingest_batch_without_enrichment_writes_nulls(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    accepted, _ = await telemetry_service.ingest_batch(
        session,
        api_key_id=api_key.id,
        user_id=user.id,
        events=[
            IngestEventIn(
                event_type="sdk.init", event_schema_version=1, payload={}
            )
        ],
        clock=FrozenClock(),
    )
    assert accepted == 1
    row = (await session.scalars(select(TelemetryEvent))).one()
    assert row.geo_region is None
    assert row.account_tier is None
    assert row.broker_operator_id is None
    assert row.ingest_node_id is None


@pytest.mark.unit
async def test_record_server_event_stamps_enrichment_columns(
    session: AsyncSession, user_and_key: tuple[User, ApiKey]
) -> None:
    user, api_key = user_and_key
    enrichment = Enrichment(ingest_node_id="ingest-server-01")
    row = await telemetry_service.record_server_event(
        session,
        event_type="server.mint_served",
        event_schema_version=1,
        payload={"capability": "x"},
        api_key_id=api_key.id,
        user_id=user.id,
        correlation_id=None,
        clock=FrozenClock(),
        enrichment=enrichment,
    )
    assert row.ingest_node_id == "ingest-server-01"
