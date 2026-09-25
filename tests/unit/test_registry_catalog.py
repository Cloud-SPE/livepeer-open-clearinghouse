"""Modules catalog fixtures exercise the network boundary and HTTP discovery contract."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest
from google.protobuf.json_format import Parse
from livepeer.registry.v1 import resolver_pb2

from livepeer_open_clearinghouse import _gen  # noqa: F401
from livepeer_open_clearinghouse.domains.discovery import service
from livepeer_open_clearinghouse.errors import DaemonUnavailable
from livepeer_open_clearinghouse.providers.registry_daemon import (
    CachingRegistryClient,
    GrpcRegistryClient,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "registry_catalog"


def fixture(name="catalog-populated-partial"):
    return Parse((FIXTURES / f"{name}.json").read_text(), resolver_pb2.ListOfferingsResult())


def client_for(proto):
    call = AsyncMock(return_value=proto)
    client = GrpcRegistryClient("/unused", discovery_rpc_timeout_seconds=1.5)
    # No ListKnown, ResolveByAddress, Select or SelectMany exists on this stub.
    client._ensure_stub = AsyncMock(return_value=SimpleNamespace(ListOfferings=call))
    return client, call


@pytest.mark.unit
async def test_partial_catalog_maps_metadata_and_identity_without_other_rpcs():
    client, call = client_for(fixture())
    caps = await service.capability_catalog(client)
    data = caps.model_dump(mode="json")
    assert data["catalog"]["completeness"] == "PARTIAL"
    assert data["catalog"]["coverage"]["known_addresses"] == 2
    assert data["catalog"]["coverage"]["deferred_addresses"] == 1
    offering = data["items"][0]["offerings"][0]
    assert offering["units_per_price"] == "60"
    assert offering["constraints"]["max_duration_seconds"] == 120
    assert offering["provider"]["worker_id"] == "broker-1"
    assert offering["provider"]["worker_url"] == "https://broker.example.invalid"
    assert offering["provider"]["health_valid_until"] is not None
    assert offering["work_unit_estimator"]["id"] == "media-duration/v1"
    assert call.call_args.kwargs == {"timeout": 1.5}
    orchs = await service.orchestrator_catalog(client, capability="example:work")
    assert orchs.items[0].eth_address == offering["provider"]["eth_address"]
    with pytest.raises(DaemonUnavailable):
        await service.orchestrator_catalog(client, capability="missing")


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["catalog-empty-partial", "catalog-uninitialized", "catalog-expired-entries"]
)
async def test_inconclusive_empty_is_unavailable(name):
    client, _ = client_for(fixture(name))
    with pytest.raises(DaemonUnavailable) as caught:
        await client.list_catalog()
    assert caught.value.status_code == 503


@pytest.mark.unit
async def test_complete_empty_is_authoritative():
    client, _ = client_for(fixture("catalog-empty-complete"))
    result = await service.capability_catalog(client)
    assert result.items == []
    assert result.catalog.completeness == "COMPLETE"


@pytest.mark.unit
@pytest.mark.parametrize("code", ["UNIMPLEMENTED", "UNAVAILABLE", "DEADLINE_EXCEEDED"])
async def test_rpc_failure_does_not_fall_back_to_crawl(code):
    client, call = client_for(fixture())
    call.side_effect = grpc.aio.AioRpcError(getattr(grpc.StatusCode, code), (), (), "private data")
    with pytest.raises(DaemonUnavailable) as caught:
        await client.list_catalog()
    assert caught.value.details["reason"] == code
    assert "private" not in str(caught.value)


@pytest.mark.unit
async def test_catalog_expiry_and_bounded_stale_fallback():
    proto = fixture()
    proto.coverage_valid_until.FromDatetime(datetime.now(UTC) + timedelta(seconds=5))
    proto.discovery_valid_until.FromDatetime(datetime.now(UTC) + timedelta(hours=1))
    client, call = client_for(proto)
    cache = CachingRegistryClient(client, ttl_seconds=300, catalog_stale_seconds=10)
    original = await cache.list_catalog()
    expiry, _ = cache._catalog_cache[("catalog",)]
    assert 0 < expiry - time.monotonic() <= 5
    cache._catalog_cache[("catalog",)] = (time.monotonic() - 1, original)
    call.side_effect = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "down")
    stale = await service.capability_catalog(cache)
    assert stale.items
    assert stale.catalog.stale is True
    assert stale.catalog.completeness == "PARTIAL"
    assert original.metadata.stale is False
    cache._catalog_cache[("catalog",)] = (time.monotonic() - 11, original)
    with pytest.raises(DaemonUnavailable):
        await cache.list_catalog()


@pytest.mark.unit
async def test_capability_and_orchestrator_http_views_share_one_refresh():
    proto = fixture()
    proto.coverage_valid_until.FromDatetime(datetime.now(UTC) + timedelta(seconds=5))
    proto.discovery_valid_until.FromDatetime(datetime.now(UTC) + timedelta(hours=1))
    client, call = client_for(proto)
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return proto

    call.side_effect = blocked
    cache = CachingRegistryClient(client, ttl_seconds=300)
    caps = asyncio.create_task(service.capability_catalog(cache))
    await started.wait()
    orchs = asyncio.create_task(service.orchestrator_catalog(cache, capability=None))
    release.set()
    assert (await caps).items
    assert (await orchs).items
    assert call.await_count == 1


@pytest.mark.unit
async def test_multiple_workers_same_payee_are_not_collapsed():
    proto = fixture()
    second = proto.entries.add()
    second.CopyFrom(proto.entries[0])
    second.worker_id = "broker-2"
    second.offering.worker_url = "https://second.example.invalid"
    client, _ = client_for(proto)
    result = await service.orchestrator_catalog(client, capability=None)
    assert len(result.items) == 2
    assert len({item.worker_url for item in result.items}) == 2


@pytest.mark.unit
async def test_http_catalog_includes_metadata():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from livepeer_open_clearinghouse.dependencies import get_authed_user, get_registry
    from livepeer_open_clearinghouse.domains.discovery.runtime import router

    app = FastAPI()
    app.include_router(router)
    client, _ = client_for(fixture())
    app.dependency_overrides[get_authed_user] = lambda: None
    app.dependency_overrides[get_registry] = lambda: client
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert body["catalog"]["completeness"] == "PARTIAL"
        assert body["items"][0]["offerings"][0]["provider"]["worker_id"] == "broker-1"
        response = await http.get("/v1/orchestrators")
        assert response.status_code == 200
        assert response.json()["catalog"]["coverage"]["known_addresses"] == 2


@pytest.mark.unit
async def test_complete_empty_cannot_become_successful_stale_empty():
    proto = fixture("catalog-empty-complete")
    proto.coverage_valid_until.FromDatetime(datetime.now(UTC) + timedelta(seconds=5))
    proto.discovery_valid_until.FromDatetime(datetime.now(UTC) + timedelta(hours=1))
    client, call = client_for(proto)
    cache = CachingRegistryClient(client, ttl_seconds=300)
    assert (await service.capability_catalog(cache)).items == []
    _, original = cache._catalog_cache[("catalog",)]
    cache._catalog_cache[("catalog",)] = (time.monotonic() - 1, original)
    call.side_effect = grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, (), (), "down")
    with pytest.raises(DaemonUnavailable):
        await service.capability_catalog(cache)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [("extra_json", b"[]"), ("constraints_json", b"[]"), ("price_per_work_unit_wei", "NaN")],
)
async def test_malformed_catalog_is_sanitized(field, value):
    proto = fixture()
    setattr(proto.entries[0].offering, field, value)
    client, _ = client_for(proto)
    with pytest.raises(DaemonUnavailable) as caught:
        await client.list_catalog()
    assert caught.value.details["reason"] == "CATALOG_MALFORMED"
