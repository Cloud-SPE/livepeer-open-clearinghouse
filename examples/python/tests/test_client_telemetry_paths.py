"""Cover the new telemetry-emitting error paths in OpenClearinghouseClient.

The happy path is already exercised by tests/test_client.py — this
file just hits the exception-emit branches that opened up when PR-6
wrapped each step of submit_job / open_session / refill_session /
close_session in try/except + emit.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from livepeer_open_clearinghouse_sdk import OpenClearinghouseClient
from livepeer_open_clearinghouse_sdk.errors import OpenClearinghouseError

pytestmark = pytest.mark.asyncio


def _captured_event_types(captured: list[dict]) -> list[str]:
    out: list[str] = []
    for batch in captured:
        for ev in batch.get("events", []):
            out.append(ev["event_type"])
    return out


async def test_submit_job_mint_failure_emits_request_error() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post("/v1/jobs").mock(
            return_value=httpx.Response(
                402,
                json={
                    "error": {
                        "code": "INSUFFICIENT_CREDIT",
                        "message": "balance too low",
                        "details": {},
                    }
                },
            )
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        with pytest.raises(OpenClearinghouseError):
            await client.submit_job(
                capability="x",
                offering="y",
                estimated_units=1,
                body={"hello": "world"},
            )
        await client.aclose()
    types = _captured_event_types(captured)
    assert "request.mint_started" in types
    assert "request.error" in types  # the failure path
    # No success-side events should have fired.
    assert "request.completed" not in types


async def test_close_session_emits_session_closed() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    sid = uuid.uuid4()
    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post(f"/v1/sessions/{sid}/close").mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_id": str(sid),
                    "work_id": "wid",
                    "actual_units": 10,
                    "billed_value_wei": 1000,
                    "refund_wei": 9000,
                    "outcome": "OVERFUNDED",
                    "closed_at": "2026-05-25T00:00:00Z",
                },
            )
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        await client.close_session(sid, actual_units=10)
        await client.aclose()
    assert "session.closed" in _captured_event_types(captured)


async def test_refill_session_402_emits_session_refill_denied() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    sid = uuid.uuid4()
    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post(f"/v1/sessions/{sid}/refill").mock(
            return_value=httpx.Response(
                402,
                json={
                    "error": {
                        "code": "cap_reached",
                        "message": "spend cap reached",
                        "details": {"which": "spend_period", "remaining_wei": "0"},
                    }
                },
            )
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        with pytest.raises(OpenClearinghouseError):
            await client.refill_session(sid)
        await client.aclose()
    types = _captured_event_types(captured)
    assert "session.refill_requested" in types
    assert "session.refill_denied" in types
    assert "session.refill_granted" not in types


async def test_refill_non_402_error_emits_session_error() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    sid = uuid.uuid4()
    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post(f"/v1/sessions/{sid}/refill").mock(
            return_value=httpx.Response(
                500,
                json={
                    "error": {
                        "code": "internal",
                        "message": "kaboom",
                        "details": {},
                    }
                },
            )
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        with pytest.raises(OpenClearinghouseError):
            await client.refill_session(sid)
        await client.aclose()
    types = _captured_event_types(captured)
    assert "session.error" in types
    assert "session.refill_denied" not in types


async def test_close_session_failure_emits_session_error() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    sid = uuid.uuid4()
    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post(f"/v1/sessions/{sid}/close").mock(
            return_value=httpx.Response(
                404,
                json={
                    "error": {
                        "code": "session_not_found",
                        "message": "no",
                        "details": {},
                    }
                },
            )
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        with pytest.raises(OpenClearinghouseError):
            await client.close_session(sid, actual_units=10)
        await client.aclose()
    assert "session.error" in _captured_event_types(captured)


async def test_open_session_emits_session_opened() -> None:
    captured: list[dict] = []

    async def _capture_telemetry(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1})

    sid = uuid.uuid4()
    payload: dict[str, Any] = {
        "session_id": str(sid),
        "work_id": "wid",
        "broker_url": "http://broker.test",
        "mode": "session-control-plus-media@v0",
        "payment_envelope": "BASE64",
        "expected_value_wei": 1000,
        "funded_value_wei": 2000,
        "refill_endpoint": f"/v1/sessions/{sid}/refill",
        "close_endpoint": f"/v1/sessions/{sid}/close",
        "opened_at": "2026-05-25T00:00:00Z",
    }
    with respx.mock(base_url="http://loc.test", assert_all_called=False) as mock:
        mock.post("/v1/sessions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        mock.post("/v1/telemetry").mock(side_effect=_capture_telemetry)
        client = OpenClearinghouseClient(
            base_url="http://loc.test", api_key="pymth_live_test"
        )
        await client.open_session(
            capability="x",
            offering="y",
            estimated_runway_units=10,
            max_total_units=100,
        )
        await client.aclose()
    assert "session.opened" in _captured_event_types(captured)
    assert "sdk.init" in _captured_event_types(captured)
