"""Tests for the SDK-side telemetry emitter."""

from __future__ import annotations

import asyncio
import gzip
import json
import uuid

import httpx
import pytest
import respx

from livepeer_open_clearinghouse_sdk.telemetry import (
    CRITICAL_EVENT_TYPES,
    DEFAULT_BATCH_SIZE,
    TelemetryEmitter,
    _is_critical,
)

pytestmark = pytest.mark.asyncio


def test_is_critical_matches_documented_set() -> None:
    # All explicit critical events.
    for et in CRITICAL_EVENT_TYPES:
        assert _is_critical(et)
    # Anything ending in .error is critical.
    assert _is_critical("request.error")
    assert _is_critical("session.error")
    assert _is_critical("custom.subsystem.error")
    # Non-criticals.
    assert not _is_critical("request.mint_started")
    assert not _is_critical("sdk.init")


async def test_emit_buffers_until_batch_or_critical() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            ingest = mock.post("/v1/telemetry").mock(
                return_value=httpx.Response(202, json={"accepted": 1})
            )
            em = TelemetryEmitter(http=http, flush_interval_seconds=999)
            em.start()
            em.emit(event_type="request.mint_started")
            # Below batch size, no flush yet.
            assert ingest.call_count == 0
            assert em.buffer_size == 1
            await em.aclose()
            # aclose drains the remainder.
            assert ingest.call_count == 1


async def test_emit_critical_flushes_immediately() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            ingest = mock.post("/v1/telemetry").mock(
                return_value=httpx.Response(202, json={"accepted": 1})
            )
            em = TelemetryEmitter(http=http, flush_interval_seconds=999)
            em.start()
            em.emit(event_type="session.refill_denied")
            # Yield enough for the flush task to run.
            for _ in range(20):
                await asyncio.sleep(0.005)
                if ingest.call_count:
                    break
            assert ingest.call_count == 1
            await em.aclose()


async def test_emit_at_batch_size_flushes() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            ingest = mock.post("/v1/telemetry").mock(
                return_value=httpx.Response(202, json={"accepted": DEFAULT_BATCH_SIZE})
            )
            em = TelemetryEmitter(http=http, flush_interval_seconds=999, batch_size=3)
            em.start()
            em.emit(event_type="request.mint_started")
            em.emit(event_type="request.mint_completed")
            em.emit(event_type="request.broker_call_started")
            for _ in range(20):
                await asyncio.sleep(0.005)
                if ingest.call_count:
                    break
            assert ingest.call_count == 1
            await em.aclose()


async def test_buffer_overflow_drops_oldest() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        em = TelemetryEmitter(http=http, flush_interval_seconds=999, buffer_cap=3, batch_size=999)
        em.emit(event_type="a.event")
        em.emit(event_type="b.event")
        em.emit(event_type="c.event")
        em.emit(event_type="d.event")  # overflow — drop oldest
        em.emit(event_type="e.event")
        assert em.buffer_size == 3
        assert em.dropped_count == 2


async def test_gzip_applied_when_body_exceeds_threshold() -> None:
    captured: dict[str, str | bytes] = {}

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured["content-encoding"] = request.headers.get("content-encoding", "")
        captured["body"] = request.content
        return httpx.Response(202, json={"accepted": 1})

    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            mock.post("/v1/telemetry").mock(side_effect=_capture)
            em = TelemetryEmitter(
                http=http,
                flush_interval_seconds=999,
                gzip_threshold_bytes=10,  # force gzip on tiny payloads
            )
            em.start()
            em.emit(event_type="request.mint_started", payload={"x": "y" * 100})
            await em.aclose()
    assert captured["content-encoding"] == "gzip"
    # Verify it's actually gzip-decompressible.
    body = gzip.decompress(captured["body"])  # type: ignore[arg-type]
    parsed = json.loads(body)
    assert parsed["events"][0]["event_type"] == "request.mint_started"


async def test_retries_on_5xx_then_drops() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            # Always 503 — should retry 3 times then give up.
            route = mock.post("/v1/telemetry").mock(return_value=httpx.Response(503))
            em = TelemetryEmitter(
                http=http,
                flush_interval_seconds=999,
                max_retries=3,
            )
            em.start()
            em.emit(event_type="session.refill_denied")  # critical → immediate
            # Give the retry loop time to exhaust attempts.
            for _ in range(60):
                await asyncio.sleep(0.05)
                if route.call_count >= 3:
                    break
            assert route.call_count == 3
            await em.aclose()


async def test_aclose_drains_remaining() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            route = mock.post("/v1/telemetry").mock(
                return_value=httpx.Response(202, json={"accepted": 2})
            )
            em = TelemetryEmitter(http=http, flush_interval_seconds=999, batch_size=999)
            em.start()
            em.emit(event_type="request.mint_started")
            em.emit(event_type="request.mint_completed")
            assert route.call_count == 0
            await em.aclose()
            assert route.call_count == 1


async def test_emit_after_close_is_silent() -> None:
    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        em = TelemetryEmitter(http=http)
        await em.aclose()
        em.emit(event_type="post.close")  # must not raise
        assert em.buffer_size == 0


async def test_event_carries_universal_fields() -> None:
    captured: list[dict] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    async with httpx.AsyncClient(base_url="http://loc.test") as http:
        with respx.mock(base_url="http://loc.test") as mock:
            mock.post("/v1/telemetry").mock(side_effect=_capture)
            em = TelemetryEmitter(http=http, flush_interval_seconds=999)
            em.start()
            cid = uuid.uuid4()
            em.emit(
                event_type="request.mint_started",
                correlation_id=cid,
                payload={"capability": "x"},
            )
            await em.aclose()
    assert captured
    ev = captured[0]["events"][0]
    assert ev["event_type"] == "request.mint_started"
    assert ev["event_schema_version"] == 1
    assert ev["correlation_id"] == str(cid)
    assert ev["client_ts"]  # ISO timestamp set
    assert ev["payload"] == {"capability": "x"}
