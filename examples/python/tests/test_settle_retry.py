"""Verify the settle-call retry helper in isolation. The full
submit_job round-trip needs to call the broker through a separate
httpx client; testing the retry helper directly keeps the assertion
focused on the new behavior."""

from __future__ import annotations

import asyncio
import pytest

import httpx

from livepeer_open_clearinghouse_sdk import OpenClearinghouseClient

pytestmark = pytest.mark.asyncio


def _make_client_with_mock(handler) -> OpenClearinghouseClient:
    """Build a client whose internal AsyncClient routes every call
    through the provided handler."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://loc.test",
        transport=transport,
        headers={
            "X-API-Key": "pymth_live_test",
            "Livepeer-Open-Clearinghouse-SDK": "python/test/dev",
        },
    )
    return OpenClearinghouseClient(
        base_url="http://loc.test", api_key="pymth_live_test", http=http
    )


async def test_post_with_retry_5xx_then_2xx(monkeypatch) -> None:
    """One 503 followed by 200 — caller sees the 200."""
    calls = 0

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    resp = await client._post_with_retry("/v1/jobs/abc/settle", json={"actual_units": 5})
    await client.aclose()
    assert resp.status_code == 200
    assert calls == 2


async def test_post_with_retry_exhausts_attempts(monkeypatch) -> None:
    """All 5xx — returns the last response after max_retries attempts."""
    calls = 0

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = _make_client_with_mock(handler)
    resp = await client._post_with_retry("/v1/jobs/abc/settle", json={"actual_units": 5})
    await client.aclose()
    assert resp.status_code == 503
    assert calls == 3  # default max_retries


async def test_post_with_retry_4xx_fails_fast() -> None:
    """A 400 doesn't trigger retry."""
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"code": "bad"}})

    client = _make_client_with_mock(handler)
    resp = await client._post_with_retry("/v1/jobs/abc/settle", json={"actual_units": 5})
    await client.aclose()
    assert resp.status_code == 400
    assert calls == 1


async def test_post_with_retry_transport_error_then_2xx(monkeypatch) -> None:
    """Transport error on first attempt, success on retry."""
    calls = 0

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("simulated network blip")
        return httpx.Response(200, json={"ok": True})

    client = _make_client_with_mock(handler)
    resp = await client._post_with_retry("/v1/jobs/abc/settle", json={"actual_units": 5})
    await client.aclose()
    assert resp.status_code == 200
    assert calls == 2


async def test_post_with_retry_transport_error_exhausts(monkeypatch) -> None:
    """Persistent transport error — raises after exhausting attempts."""
    calls = 0

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("blip")

    client = _make_client_with_mock(handler)
    with pytest.raises(httpx.ConnectError):
        await client._post_with_retry(
            "/v1/jobs/abc/settle", json={"actual_units": 5}, max_retries=2
        )
    await client.aclose()
    assert calls == 2
