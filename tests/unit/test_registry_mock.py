"""Unit tests for MockRegistryClient."""

from __future__ import annotations

import pytest

from pymthouse.providers.registry_daemon import MockRegistryClient


@pytest.mark.unit
async def test_list_capabilities_returns_sample() -> None:
    client = MockRegistryClient()
    caps = await client.list_capabilities()
    assert len(caps) >= 1
    names = {c.name for c in caps}
    assert "openai:chat-completions" in names
    assert "livepeer:transcoder/h264" in names


@pytest.mark.unit
async def test_select_finds_matching_route() -> None:
    client = MockRegistryClient()
    r = await client.select("openai:chat-completions", "gpt-oss-20b")
    assert r is not None
    assert r.work_unit == "token"
    assert r.eth_address.startswith("0x")
    assert r.quote_id != ""


@pytest.mark.unit
async def test_select_returns_none_for_unknown() -> None:
    client = MockRegistryClient()
    assert await client.select("does-not-exist", "neither") is None


@pytest.mark.unit
async def test_list_orchestrators_filter_by_capability() -> None:
    client = MockRegistryClient()
    all_orchs = await client.list_orchestrators()
    filtered = await client.list_orchestrators(capability="openai:chat-completions")
    assert len(filtered) <= len(all_orchs)
    for o in filtered:
        assert any(c.name == "openai:chat-completions" for c in o.capabilities)
