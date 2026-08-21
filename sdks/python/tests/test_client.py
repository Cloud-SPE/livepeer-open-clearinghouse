"""Tests for the handoff-mode Python SDK.

Stubs the LOC gateway + broker via respx. Covers:
  - submit_job happy path (POST /v1/jobs + broker call + settle)
  - submit_job error mappings (insufficient credit, no route)
  - open_session returning a SessionHandle
  - refill_session / close_session / get_session_status
"""

from __future__ import annotations

import base64
import json
import uuid

import httpx
import pytest
import respx

from livepeer_open_clearinghouse_sdk import (
    BrokerProtocolError,
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


def _job_open(
    job_id: str = "00000000-0000-0000-0000-000000000001",
    *,
    transport: str = "unary",
) -> dict:
    return {
        "job_id": job_id,
        "request_id": "broker-request-1",
        "work_id": "wid-abc",
        "broker_url": BROKER,
        "protocol": "paid-job/v1",
        "transport": transport,
        "work_unit": "token",
        "payment_envelope": "BASE64ENVELOPE",
        "expected_value_wei": 100_000,
        "funded_value_wei": 100_000,
        "settle_endpoint": f"/v1/jobs/{job_id}/settle",
        "opened_at": "2026-05-24T12:00:00Z",
    }


def _broker_headers(units: int, *, job_id: str = "broker-job-1") -> dict[str, str]:
    return {
        "Livepeer-Work-Units": str(units),
        "Livepeer-Work-Unit": "token",
        "Livepeer-Job-Id": job_id,
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
    respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(201, json=_job_open(jid)))
    respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(
            200,
            json={"reply": "ok"},
            headers=_broker_headers(42),
        )
    )
    settle_call = respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
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
    assert result.protocol == "paid-job/v1"
    assert result.transport == "unary"
    assert result.work_unit == "token"
    assert result.broker_job_id == "broker-job-1"
    assert result.request_id == "broker-request-1"
    assert json.loads(settle_call.calls[0].request.content) == {
        "actual_units": 42,
        "broker_job_id": "broker-job-1",
        "work_unit": "token",
    }


@respx.mock
async def test_submit_job_forwards_livepeer_headers_to_broker() -> None:
    jid = "00000000-0000-0000-0000-000000000bcd"
    loc_call = respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid))
    )
    broker_call = respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(
            200,
            json={"x": 1},
            headers=_broker_headers(10),
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
    assert hdrs["livepeer-protocol"] == "paid-job/v1"
    assert "livepeer-mode" not in hdrs
    assert "livepeer-spec-version" not in hdrs
    assert hdrs["livepeer-request-id"] == "broker-request-1"
    assert loc_call.calls[0].request.headers["idempotency-key"] == "req-12345"
    assert json.loads(loc_call.calls[0].request.content)["transport"] == "unary"


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
    respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(201, json=_job_open(jid)))
    respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(
            429,
            json={"error": "rate_limited"},
            headers=_broker_headers(0),
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


@respx.mock
async def test_submit_job_stream_selects_sse_and_settles_terminal_units() -> None:
    jid = "00000000-0000-0000-0000-000000000eed"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid, transport="stream"))
    )
    stream_headers = _broker_headers(7)
    del stream_headers["Livepeer-Work-Units"]
    stream_headers["Trailer"] = "Livepeer-Work-Units"
    signed_settlement = {
        "payload": {"work_id": "wid-abc", "debited_units": "7"},
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "jcs",
            "value": "0xsigned",
        },
    }
    encoded_settlement = base64.b64encode(json.dumps(signed_settlement).encode()).decode()
    broker_call = respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(
            200,
            content=b"data: hello\n\n",
            headers={"Content-Type": "text/event-stream", **stream_headers},
        )
    )
    settlement_query = respx.get(f"{BROKER}/v1/settlement/broker-job-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "broker-job-1",
                "state": "terminal",
                "work_units": 7,
                "unit": "tokens",
            },
            headers={**_broker_headers(7), "Livepeer-Settlement": encoded_settlement},
        )
    )
    settle_call = respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=7))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        result = await client.submit_job(
            capability="x",
            offering="x",
            estimated_units=10,
            body={"prompt": "hello"},
            transport="stream",
        )
    assert len(broker_call.calls) == 1
    first_request = broker_call.calls[0].request
    assert first_request.headers["accept"] == "text/event-stream"
    assert len(settlement_query.calls) == 1
    assert result.body == "data: hello\n\n"
    assert result.transport == "stream"
    assert result.actual_units == 7
    assert json.loads(settle_call.calls[0].request.content)["settlement"] == signed_settlement


@respx.mock
async def test_submit_job_multipart_forwards_preencoded_content_type() -> None:
    jid = "00000000-0000-0000-0000-000000000eef"
    respx.post(f"{BASE}/v1/jobs").mock(
        return_value=httpx.Response(201, json=_job_open(jid, transport="multipart"))
    )
    broker_call = respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(200, json={"ok": True}, headers=_broker_headers(2))
    )
    respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(
        return_value=httpx.Response(200, json=_job_settled(jid, actual=2))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await client.submit_job(
            capability="x",
            offering="x",
            estimated_units=2,
            body=b"--boundary--",
            transport="multipart",
            content_type="multipart/form-data; boundary=boundary",
        )
    assert broker_call.calls[0].request.headers["content-type"] == (
        "multipart/form-data; boundary=boundary"
    )


@respx.mock
async def test_submit_job_rejects_broker_work_unit_drift_without_settling() -> None:
    jid = "00000000-0000-0000-0000-000000000efa"
    respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(201, json=_job_open(jid)))
    headers = _broker_headers(3)
    headers["Livepeer-Work-Unit"] = "frames"
    respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(200, json={"ok": True}, headers=headers)
    )
    settle_call = respx.post(f"{BASE}/v1/jobs/{jid}/settle").mock(return_value=httpx.Response(500))

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        with pytest.raises(BrokerProtocolError) as exc_info:
            await client.submit_job(capability="x", offering="x", estimated_units=3, body={})
    assert exc_info.value.code == "work_unit_mismatch"
    assert not settle_call.called


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
    assert json.loads(route.calls[0].request.content) == {"observed_consumed_units": 80}


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
    respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(201, json=_job_open(jid)))
    broker_call = respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(200, json={"x": 1}, headers=_broker_headers(5))
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
    respx.post(f"{BASE}/v1/jobs").mock(return_value=httpx.Response(201, json=_job_open(jid)))
    respx.post(f"{BROKER}/v1/job").mock(
        return_value=httpx.Response(
            200,
            content=b"plain text",
            headers={"Content-Type": "text/plain", **_broker_headers(1)},
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
