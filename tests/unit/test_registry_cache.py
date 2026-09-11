"""Unit tests for CachingRegistryClient.

We wrap a counting-instrumented MockRegistryClient and watch the call
counts to verify hits, misses, expiry, and the "don't cache None"
behavior.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from livepeer_open_clearinghouse.providers.registry_daemon import (
    CachingRegistryClient,
    MockRegistryClient,
)


class _Counted(MockRegistryClient):
    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.calls: dict[str, int] = {}

    async def select(self, capability, offering):  # type: ignore[override]
        self.calls["select"] = self.calls.get("select", 0) + 1
        return await super().select(capability, offering)

    async def select_many(self, capability, offering):  # type: ignore[override]
        self.calls["select_many"] = self.calls.get("select_many", 0) + 1
        return await super().select_many(capability, offering)

    async def list_capabilities(self):  # type: ignore[override]
        self.calls["list_capabilities"] = self.calls.get("list_capabilities", 0) + 1
        return await super().list_capabilities()

    async def list_orchestrators(self, *, capability=None):  # type: ignore[override]
        self.calls["list_orchestrators"] = self.calls.get("list_orchestrators", 0) + 1
        return await super().list_orchestrators(capability=capability)


@pytest.mark.unit
async def test_ttl_zero_disables_cache() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=0)
    for _ in range(3):
        await cache.list_capabilities()
    assert inner.calls["list_capabilities"] == 3


@pytest.mark.unit
async def test_select_hits_cache_on_repeat() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    for _ in range(5):
        await cache.select("openai:chat-completions", "gpt-oss-20b")
    assert inner.calls["select"] == 1


@pytest.mark.unit
async def test_select_distinct_keys_each_miss_once() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    await cache.select("a", "b")
    await cache.select("a", "c")  # different offering
    await cache.select("d", "b")  # different capability
    assert inner.calls["select"] == 3


@pytest.mark.unit
async def test_select_none_results_are_not_cached() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    # "nope" / "neither" doesn't match anything in the sample set.
    assert await cache.select("nope", "neither") is None
    assert await cache.select("nope", "neither") is None
    assert inner.calls["select"] == 2  # both calls hit the inner client


@pytest.mark.unit
async def test_list_capabilities_cached() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    for _ in range(4):
        await cache.list_capabilities()
    assert inner.calls["list_capabilities"] == 1


@pytest.mark.unit
async def test_invalidate_drops_cache() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    await cache.list_capabilities()
    cache.invalidate()
    await cache.list_capabilities()
    assert inner.calls["list_capabilities"] == 2


@pytest.mark.unit
async def test_ttl_expiry_refetches() -> None:
    inner = _Counted()
    cache = CachingRegistryClient(inner, ttl_seconds=1)
    await cache.list_capabilities()
    await asyncio.sleep(1.1)
    await cache.list_capabilities()
    assert inner.calls["list_capabilities"] == 2


@pytest.mark.unit
async def test_health_bypasses_cache() -> None:
    inner = _Counted()
    inner.health = AsyncMock(return_value=True)  # type: ignore[method-assign]
    cache = CachingRegistryClient(inner, ttl_seconds=60)

    assert await cache.health() is True
    assert await cache.health() is True
    assert inner.health.await_count == 2
