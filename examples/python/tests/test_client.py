"""Tests for the handoff-mode Python SDK.

Stubs the LOC gateway + broker via respx. Covers:
  - submit_job happy path (POST /v1/jobs + broker call + settle)
  - submit_job error mappings (insufficient credit, no route)
  - open_session returning a SessionHandle
  - refill_session / close_session / get_session_status
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import respx

from livepeer_open_clearinghouse_sdk import (
    CapStatus,
    InsufficientCredit,
    JobResult,
    NoRouteAvailable,
    OpenClearinghouseClient,
    OpenClearinghouseError,
    SessionHandle,
    is_open_clearinghouse_error,
)

BASE = "http://loc.test"
BROKER = "https://broker.example/livepeer"
KEY = "pymth_live_test_key_value"


def _job_open(job_id: str = "00000000-0000-0000-0000-000000000001") -> dict:
    return {
        "job_id": job_id,
        "work_id": "wid-abc",
        "broker_url": BROKER,
        "mode": "http-reqresp@v0",
        "payment_envelope": "BASE64ENVELOPE",
        "expected_value_wei": 100_000,
        "funded_value_wei": 100_000,
        "settle_endpoint": f"/v1/jobs/{job_id}/settle",
        "opened_at": "2026-05-24T12:00:00Z",
    }


def _job_settled(job_id: str, actual: int = 42) -> dict:
    return {
        "job_id": job_id,
        "work_id": "wid-abc",
        "actual_units": actual,
        "billed_value_wei": actual * 1000,
        "refund_wei": 100_000 - actual * 1000,
        "outcome": "OVERFUNDED",
        "closed_at": "2026-05-24T12:00:30Z",
        "cap_status": {
            "session_pct_used": actual / 100,
            "spend_period_pct_used": None,
            "user_balance_pct_used": None,
            "operator_pool_pct_used": None,
            "will_refuse_next_refill": False,
            "winddown_reason": None,
        },
    }


# ----- constructor --------------------------------------------------


def test_constructor_rejects_obviously_wrong_key() -> None:
    with pytest.raises(ValueError):
        OpenClearinghouseClient(base_url="https://x", api_key="nope")


@respx.mock
async def test_constructor_sets_sdk_identity_header() -> None:
    route = respx.get(f"{BASE}/v1/capabilities").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await client.list_capabilities()
    seen = route.calls[0].request.headers
    assert seen.get("livepeer-open-clearinghouse-sdk", "").startswith("python/")


# ----- submit_job ---------------------------------------------------


@respx.mock
async def test_submit_job_happy_path() -> None:
    jid = "00000000-0000-0000-0000-000000000abc"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    respx.post(f"{BROKER}/v1/cap").mock(
        return_value=httpx.Response(
            200,
            json={"reply": "ok"},
            headers={"Livepeer-Work-Units": "42"},
        )
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=42))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.submit_job(
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=80,
            max_total_units=100,
            body={"prompt": "hello"},
        )
    assert isinstance(result, JobResult)
    assert result.status == 200
    assert result.actual_units == 42
    assert result.billed_value_wei == 42_000
    assert result.refund_wei == 58_000
    assert result.outcome == "OVERFUNDED"
    assert isinstance(result.cap_status, CapStatus)
    assert result.cap_status.session_pct_used == pytest.approx(0.42)
    assert result.body == {"reply": "ok"}


@respx.mock
async def test_submit_job_forwards_livepeer_headers_to_broker() -> None:
    jid = "00000000-0000-0000-0000-000000000bcd"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    broker_call = respx.post(f"{BROKER}/v1/cap").mock(
        return_value=httpx.Response(
            200,
            json={"x": 1},
            headers={"Livepeer-Work-Units": "10"},
        )
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=10))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await client.submit_job(
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            estimated_units=10,
            body={"x": 1},
            request_id="req-12345",
        )

    hdrs = broker_call.calls[0].request.headers
    assert hdrs["livepeer-capability"] == "openai:chat-completions"
    assert hdrs["livepeer-offering"] == "gpt-oss-20b"
    assert hdrs["livepeer-payment"] == "BASE64ENVELOPE"
    assert hdrs["livepeer-mode"] == "http-reqresp@v0"
    assert hdrs["livepeer-request-id"] == "req-12345"


@respx.mock
async def test_submit_job_maps_insufficient_credit() -> None:
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(
            402,
            json={
                "error": {
                    "code": "INSUFFICIENT_CREDIT",
                    "message": "broke",
                    "details": {"available_wei": "0", "required_wei": "1000"},
                }
            },
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        with pytest.raises(InsufficientCredit) as exc_info:
            await client.submit_job(
                capability="x",
                offering="x",
                estimated_units=1,
                body={},
            )
    assert exc_info.value.status == 402
    assert is_open_clearinghouse_error(exc_info.value)


@respx.mock
async def test_submit_job_maps_no_route() -> None:
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(
            404, json={"error": {"code": "NO_ROUTE_AVAILABLE", "message": "no orch"}}
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        with pytest.raises(NoRouteAvailable):
            await client.submit_job(
                capability="x",
                offering="x",
                estimated_units=1,
                body={},
            )


@respx.mock
async def test_submit_job_passes_through_broker_4xx() -> None:
    """Broker-side non-2xx is reported in JobResult.status, not raised."""
    jid = "00000000-0000-0000-0000-000000000def"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    respx.post(f"{BROKER}/v1/cap").mock(
        return_value=httpx.Response(
            429,
            json={"error": "rate_limited"},
            headers={"Livepeer-Work-Units": "0"},
        )
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=0))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.submit_job(
            capability="x",
            offering="x",
            estimated_units=1,
            body={},
        )
    assert result.status == 429
    assert result.actual_units == 0


# ----- discovery ---------------------------------------------------


@respx.mock
async def test_list_capabilities_unwraps_items() -> None:
    respx.get(f"{BASE}/v1/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"name": "openai:embeddings", "work_unit": "token", "offerings": []}]},
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        caps = await client.list_capabilities()
    assert caps[0]["name"] == "openai:embeddings"


@respx.mock
async def test_list_orchestrators_passes_capability_filter() -> None:
    route = respx.get(f"{BASE}/v1/orchestrators").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await client.list_orchestrators(capability="video:transcode.live")
    assert "capability=video%3Atranscode.live" in str(route.calls[0].request.url)


# ----- sessions (case d) ------------------------------------------


@respx.mock
async def test_open_session_returns_handle() -> None:
    sid = "00000000-0000-0000-0000-000000000999"
    respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(
            201,
            json={
                "session_id": sid,
                "work_id": "wid-sess",
                "broker_url": BROKER,
                "mode": "session-control-plus-media@v0",
                "payment_envelope": "BASE64SESSION",
                "expected_value_wei": 100_000,
                "funded_value_wei": 200_000,
                "refill_endpoint": f"/v1/sessions/{sid}/refill",
                "close_endpoint": f"/v1/sessions/{sid}/close",
                "opened_at": "2026-05-24T12:00:00Z",
            },
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        handle = await client.open_session(
            capability="livepeer:vtuber-session",
            offering="vtuber-1080p30",
            estimated_runway_units=100,
            max_total_units=200,
        )
    assert isinstance(handle, SessionHandle)
    assert handle.broker_url == BROKER
    assert handle.mode == "session-control-plus-media@v0"
    assert handle.funded_value_wei == 200_000


@respx.mock
async def test_refill_session_posts_observed_consumed() -> None:
    sid = uuid.uuid4()
    route = respx.post(f"{BASE}/v1/sessions/{sid}/refill").mock(
        return_value=httpx.Response(
            200,
            json={
                "work_id": "wid",
                "refill_seq": 3,
                "payment_envelope": "REFILLENV",
                "expected_value_wei": 10_000,
                "funded_value_wei": 10_000,
                "cap_status": {
                    "session_pct_used": 0.5,
                    "spend_period_pct_used": None,
                    "user_balance_pct_used": None,
                    "operator_pool_pct_used": None,
                    "will_refuse_next_refill": False,
                    "winddown_reason": None,
                },
            },
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.refill_session(sid, observed_consumed_units=80)
    assert result["payment_envelope"] == "REFILLENV"
    assert json.loads(route.calls[0].request.content) == {
        "observed_consumed_units": 80
    }


@respx.mock
async def test_close_session_threads_outcome() -> None:
    sid = uuid.uuid4()
    route = respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": str(sid),
                "work_id": "w",
                "actual_units": 100,
                "billed_value_wei": 100_000,
                "refund_wei": 0,
                "outcome": "EXACT",
                "closed_at": "2026-05-24T12:30:00Z",
            },
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.close_session(sid, actual_units=100, outcome="EXACT")
    assert result["outcome"] == "EXACT"
    body = json.loads(route.calls[0].request.content)
    assert body == {"actual_units": 100, "outcome": "EXACT"}


@respx.mock
async def test_submit_job_sends_bytes_body_as_octet_stream() -> None:
    """Bytes body uses application/octet-stream."""
    jid = "00000000-0000-0000-0000-0000000000ff"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    broker_call = respx.post(f"{BROKER}/v1/cap").mock(
        return_value=httpx.Response(200, json={"x": 1}, headers={"Livepeer-Work-Units": "5"})
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=5))
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await client.submit_job(
            capability="openai:audio-speech",
            offering="kokoro",
            estimated_units=5,
            body=b"\x00\x01\x02",
        )
    assert broker_call.calls[0].request.headers["content-type"] == "application/octet-stream"


@respx.mock
async def test_submit_job_parses_text_body_when_not_json() -> None:
    jid = "00000000-0000-0000-0000-0000000000aa"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    respx.post(f"{BROKER}/v1/cap").mock(
        return_value=httpx.Response(
            200,
            content=b"plain text",
            headers={"Content-Type": "text/plain", "Livepeer-Work-Units": "1"},
        )
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=1))
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.submit_job(
            capability="x",
            offering="x",
            estimated_units=1,
            body={"x": 1},
        )
    assert result.body == "plain text"


@respx.mock
async def test_unwrap_handles_non_json_error_body() -> None:
    respx.get(f"{BASE}/v1/capabilities").mock(
        return_value=httpx.Response(500, content=b"<html>500</html>")
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        with pytest.raises(OpenClearinghouseError) as exc:
            await client.list_capabilities()
    assert exc.value.status == 500


def test_wei_to_eth_decimal_math() -> None:
    from livepeer_open_clearinghouse_sdk import wei_to_eth

    assert wei_to_eth(10**18) == 1
    assert wei_to_eth(5 * 10**17) == pytest.approx(0.5)


@respx.mock
async def test_get_session_status_round_trip() -> None:
    sid = uuid.uuid4()
    respx.get(f"{BASE}/v1/sessions/{sid}").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": str(sid),
                "work_id": "w",
                "capability": "c",
                "offering": "o",
                "mode": "ws-realtime@v0",
                "state": "open",
                "estimated_units": 100,
                "max_total_units": 1000,
                "funded_value_wei": 1_000_000,
                "billed_value_wei": 100_000,
                "refill_count": 0,
                "cap_status": None,
                "opened_at": "2026-05-24T12:00:00Z",
                "closed_at": None,
                "actual_units": None,
                "outcome": None,
            },
        )
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.get_session_status(sid)
    assert result["state"] == "open"
