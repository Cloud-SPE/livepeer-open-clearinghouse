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


@pytest.mark.unit
async def test_select_many_missing_then_recovered_is_not_negative_cached() -> None:
    inner = MockRegistryClient()
    routes = await inner.select_many("openai:chat-completions", "gpt-oss-20b")
    inner.select_many = AsyncMock(side_effect=[[], routes])
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    assert await cache.select_many("a", "b") == []
    assert await cache.select_many("a", "b") == routes
    assert await cache.select_many("a", "b") == routes
    assert inner.select_many.await_count == 2


@pytest.mark.unit
async def test_concurrent_catalog_requests_share_refresh_and_survive_cancellation() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    inner = MockRegistryClient()
    expected = await inner.list_capabilities()

    async def blocked():
        entered.set()
        await release.wait()
        return expected

    inner.list_capabilities = AsyncMock(side_effect=blocked)
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    first = asyncio.create_task(cache.list_capabilities())
    await entered.wait()
    second = asyncio.create_task(cache.list_capabilities())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == expected
    assert await cache.list_capabilities() == expected
    assert inner.list_capabilities.await_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("method", ["list_capabilities", "list_orchestrators"])
async def test_catalog_timeout_is_unavailable_and_retries(method) -> None:
    from livepeer_open_clearinghouse.errors import DaemonUnavailable

    async def blocked():
        await asyncio.Event().wait()

    inner = MockRegistryClient()
    fetch = AsyncMock(side_effect=blocked)
    setattr(inner, method, fetch)
    cache = CachingRegistryClient(inner, ttl_seconds=0, catalog_timeout_seconds=0.01)
    with pytest.raises(DaemonUnavailable) as caught:
        await getattr(cache, method)()
    assert caught.value.status_code == 503
    fetch.side_effect = None
    fetch.return_value = []
    assert await getattr(cache, method)() == []
    assert fetch.await_count == 2


@pytest.mark.unit
async def test_stale_catalog_fallback_has_fixed_expiry_and_does_not_affect_selection() -> None:
    import time

    from livepeer_open_clearinghouse.errors import DaemonUnavailable

    inner = MockRegistryClient()
    cache = CachingRegistryClient(inner, ttl_seconds=60, catalog_stale_seconds=30)
    expected = await cache.list_capabilities()
    key = ("catalog",)
    snapshot = cache._catalog_cache[key][1]
    expiry = time.monotonic() - 1
    cache._catalog_cache[key] = (expiry, snapshot)
    inner.list_capabilities = AsyncMock(
        side_effect=DaemonUnavailable(daemon="registry", reason="UNAVAILABLE")
    )
    inner.select_many = AsyncMock(
        side_effect=DaemonUnavailable(daemon="registry", reason="UNAVAILABLE")
    )
    assert await cache.list_capabilities() == expected
    assert cache._catalog_cache[key][0] == expiry
    with pytest.raises(DaemonUnavailable):
        await cache.select_many("video:transcode.abr", "abr-default")
    cache._catalog_cache[key] = (time.monotonic() - 31, snapshot)
    with pytest.raises(DaemonUnavailable):
        await cache.list_capabilities()


@pytest.mark.unit
async def test_invalidation_during_refresh_does_not_repopulate_cache() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    inner = MockRegistryClient()

    async def blocked():
        entered.set()
        await release.wait()
        return []

    inner.list_capabilities = AsyncMock(side_effect=blocked)
    cache = CachingRegistryClient(inner, ttl_seconds=60)
    request = asyncio.create_task(cache.list_capabilities())
    await entered.wait()
    cache.invalidate()
    release.set()
    assert await request == []
    assert cache._catalog_cache == {}
