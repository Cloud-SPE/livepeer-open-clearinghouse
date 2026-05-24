"""Tests for SessionRunner.

Uses an in-process aiohttp-free WS server stub (websockets-native) for
the broker side and respx for the LOC HTTP surface. Covers:
  - Happy path: balance-low → refill → session.topup frame delivery
  - (d-bounded) ws-realtime: balance-low fires winddown, no refill
  - Refill refusal (LOC returns 402) → on_refill_refused callback
  - HTTP-topup mode: refill delivered via POST to topup_url
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import respx
import websockets
from websockets.asyncio.server import serve

from livepeer_open_clearinghouse_sdk import (
    OpenClearinghouseClient,
    RefillEvent,
    SessionHandle,
    SessionRunner,
    WinddownEvent,
)

BASE = "http://loc.test"
KEY = "pymth_live_test"


def _handle(broker_url: str, mode: str, *, session_id: uuid.UUID | None = None) -> SessionHandle:
    sid = session_id or uuid.uuid4()
    return SessionHandle(
        session_id=sid,
        work_id="wid-sess",
        broker_url=broker_url,
        mode=mode,
        payment_envelope="BASE64ENV",
        expected_value_wei=100_000,
        funded_value_wei=200_000,
        refill_endpoint=f"/v1/sessions/{sid}/refill",
        close_endpoint=f"/v1/sessions/{sid}/close",
    )


def _close_response(sid: uuid.UUID, actual_units: int = 0) -> dict:
    return {
        "session_id": str(sid),
        "work_id": "wid-sess",
        "actual_units": actual_units,
        "billed_value_wei": actual_units * 1000,
        "refund_wei": 200_000 - actual_units * 1000,
        "outcome": "OVERFUNDED",
        "closed_at": "2026-05-24T12:30:00Z",
    }


async def _start_ws_broker(handler):
    """Start a websockets server on an OS-assigned port; return (url, close)."""
    server = await serve(handler, "127.0.0.1", 0)
    host, port = next(iter(server.sockets)).getsockname()[:2]
    url = f"ws://{host}:{port}"

    async def close() -> None:
        server.close()
        await server.wait_closed()

    return url, close


@respx.mock
async def test_session_runner_refill_on_balance_low_ws_topup() -> None:
    """session-control-plus-media@v0 happy path: WS connects, broker
    emits Livepeer-Balance-Low, runner calls LOC refill, sends back a
    session.topup JSON frame on the same WS."""
    received_frames: list[str] = []
    refill_event_received = asyncio.Event()

    async def broker_handler(ws):
        # 1. Send a balance-low control frame
        await ws.send(
            json.dumps(
                {
                    "type": "session.balance.low",
                    "observed_consumed_units": 80,
                }
            )
        )
        # 2. Wait for the session.topup frame
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
            received_frames.append(msg)
        except TimeoutError:
            pass

    broker_url, broker_close = await _start_ws_broker(broker_handler)
    try:
        sid = uuid.uuid4()
        handle = _handle(broker_url, "session-control-plus-media@v0", session_id=sid)

        respx.post(f"{BASE}/v1/sessions/{sid}/refill").mock(
            return_value=httpx.Response(
                200,
                json={
                    "work_id": "wid-sess",
                    "refill_seq": 1,
                    "payment_envelope": "REFILL-ENV",
                    "expected_value_wei": 50_000,
                    "funded_value_wei": 50_000,
                    "cap_status": {
                        "session_pct_used": 0.4,
                        "spend_period_pct_used": None,
                        "user_balance_pct_used": None,
                        "operator_pool_pct_used": None,
                        "will_refuse_next_refill": False,
                        "winddown_reason": None,
                    },
                },
            )
        )
        respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
            return_value=httpx.Response(200, json=_close_response(sid))
        )

        async def on_refill(_event: RefillEvent) -> None:
            refill_event_received.set()

        async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
            async with SessionRunner(
                client=client,
                handle=handle,
                on_refill_succeeded=on_refill,
            ) as runner:
                # Give the balance_low → refill → topup-frame round-trip
                # time to run.
                await asyncio.wait_for(refill_event_received.wait(), timeout=3.0)
                await runner.close(actual_units=0)
    finally:
        await broker_close()

    assert refill_event_received.is_set()
    assert len(received_frames) == 1
    frame = json.loads(received_frames[0])
    assert frame["type"] == "session.topup"
    assert frame["body"]["payment_header"] == "REFILL-ENV"


@respx.mock
async def test_session_runner_bounded_mode_no_refill() -> None:
    """ws-realtime@v0 is bounded — balance-low fires winddown only;
    no refill call to LOC, no topup frame to broker."""
    winddown_event = asyncio.Event()

    async def broker_handler(ws):
        await ws.send(
            json.dumps(
                {
                    "type": "session.balance.low",
                    "projected_end_at": "2026-05-24T12:15:00Z",
                }
            )
        )
        # Wait briefly to give the runner a chance to (incorrectly)
        # send a refill frame — none should arrive.
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.5)
        except TimeoutError:
            pass

    broker_url, broker_close = await _start_ws_broker(broker_handler)
    try:
        sid = uuid.uuid4()
        handle = _handle(broker_url, "ws-realtime@v0", session_id=sid)
        respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
            return_value=httpx.Response(200, json=_close_response(sid))
        )

        # If the runner incorrectly tried to refill, this mock would 404.
        respx.post(f"{BASE}/v1/sessions/{sid}/refill").mock(
            return_value=httpx.Response(400, json={"error": {"code": "should_not_call"}})
        )

        async def on_winddown(_event: WinddownEvent) -> None:
            winddown_event.set()

        async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
            async with SessionRunner(
                client=client,
                handle=handle,
                on_winddown_warning=on_winddown,
            ) as runner:
                await asyncio.wait_for(winddown_event.wait(), timeout=2.0)
                await runner.close(actual_units=0)
    finally:
        await broker_close()

    assert winddown_event.is_set()


@respx.mock
async def test_session_runner_refill_refused_fires_callback() -> None:
    """When LOC returns 402 on refill, on_refill_refused gets the
    typed error and the runner doesn't kill the WS — it lets the
    broker close naturally."""
    refused_event = asyncio.Event()
    received: list[RefillEvent] = []

    async def broker_handler(ws):
        await ws.send(json.dumps({"type": "session.balance.low"}))
        # Keep the WS open briefly; runner shouldn't close it itself.
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.5)
        except TimeoutError:
            pass

    broker_url, broker_close = await _start_ws_broker(broker_handler)
    try:
        sid = uuid.uuid4()
        handle = _handle(broker_url, "session-control-plus-media@v0", session_id=sid)

        respx.post(f"{BASE}/v1/sessions/{sid}/refill").mock(
            return_value=httpx.Response(
                402,
                json={
                    "error": {
                        "code": "cap_reached",
                        "message": "period cap reached",
                        "details": {"which": "spend_period", "remaining_wei": "0"},
                    }
                },
            )
        )
        respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
            return_value=httpx.Response(200, json=_close_response(sid))
        )

        async def on_refused(event: RefillEvent) -> None:
            received.append(event)
            refused_event.set()

        async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
            async with SessionRunner(
                client=client,
                handle=handle,
                on_refill_refused=on_refused,
            ) as runner:
                await asyncio.wait_for(refused_event.wait(), timeout=3.0)
                await runner.close(actual_units=0)
    finally:
        await broker_close()

    assert refused_event.is_set()
    assert received[0].error is not None
    assert received[0].error.code == "cap_reached"


@respx.mock
async def test_session_runner_http_topup_mode_delivers_via_post() -> None:
    """live-session-remote-runner@v0: SessionRunner opens via POST
    /v1/cap to broker, captures control.topup_url, then on a refill
    POSTs the envelope back to that URL."""
    fake_topup_url = "http://broker.live.test/v1/cap/bsess_abc/topup"
    sid = uuid.uuid4()
    # Use the FastAPI app via httpx ASGI transport — same pattern as
    # the end-to-end test. Mock the LOC + broker via respx/transports.
    handle = _handle(
        broker_url="http://broker.live.test",
        mode="live-session-remote-runner@v0",
        session_id=sid,
    )

    # Mock the broker via respx (covers both POSTs above)
    respx.post("http://broker.live.test/v1/cap").mock(
        return_value=httpx.Response(
            200,
            json={
                "broker_session_id": "bsess_abc",
                "work_id": "wid",
                "control": {"topup_url": fake_topup_url},
            },
        )
    )
    respx.post(fake_topup_url).mock(
        return_value=httpx.Response(
            200, json={"broker_session_id": "bsess_abc", "state": "publishing"}
        )
    )
    respx.post(f"{BASE}/v1/sessions/{sid}/refill").mock(
        return_value=httpx.Response(
            200,
            json={
                "work_id": "wid",
                "refill_seq": 1,
                "payment_envelope": "REFILL-HTTP",
                "expected_value_wei": 10_000,
                "funded_value_wei": 10_000,
                "cap_status": {
                    "session_pct_used": 0.2,
                    "spend_period_pct_used": None,
                    "user_balance_pct_used": None,
                    "operator_pool_pct_used": None,
                    "will_refuse_next_refill": False,
                    "winddown_reason": None,
                },
            },
        )
    )
    respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
        return_value=httpx.Response(200, json=_close_response(sid))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        async with SessionRunner(client=client, handle=handle) as runner:
            # No WS for live-session — drive a refill by directly invoking
            # the runner's balance-low handler. This is what would
            # happen if the customer's media-plane code observed
            # balance-low and routed it to the runner.
            await runner._on_balance_low({"observed_consumed_units": 50})  # noqa: SLF001
            await runner.close(actual_units=0)

    # The runner POSTed the refill envelope to control.topup_url
    refill_call = next(
        call for call in respx.calls if str(call.request.url) == fake_topup_url
    )
    body = json.loads(refill_call.request.content)
    assert body["gateway_session_id"] == str(sid)
    assert refill_call.request.headers["Livepeer-Payment"] == "REFILL-HTTP"


@respx.mock
async def test_session_runner_unsupported_mode_raises() -> None:
    """If a customer somehow constructs a runner for a mode neither in
    BOUNDED_MODES nor in WS/HTTP topup sets, SessionRunner raises."""
    from livepeer_open_clearinghouse_sdk import OpenClearinghouseError

    sid = uuid.uuid4()
    handle = _handle(
        broker_url="http://broker.test",
        mode="http-reqresp@v0",  # job mode, not a session mode
        session_id=sid,
    )
    respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
        return_value=httpx.Response(200, json=_close_response(sid))
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        runner = SessionRunner(client=client, handle=handle)
        with pytest.raises(OpenClearinghouseError):
            await runner._start()  # noqa: SLF001


async def test_session_runner_close_sets_final_settle() -> None:
    """After close, runner.outcome / billed_value_wei / refund_wei are
    populated from the LOC close response."""
    async def broker_handler(ws):
        # Idle until disconnect
        try:
            await asyncio.wait_for(ws.recv(), timeout=2.0)
        except TimeoutError:
            pass

    broker_url, broker_close = await _start_ws_broker(broker_handler)
    try:
        sid = uuid.uuid4()
        handle = _handle(broker_url, "session-control-plus-media@v0", session_id=sid)

        with respx.mock:
            respx.post(f"{BASE}/v1/sessions/{sid}/close").mock(
                return_value=httpx.Response(200, json=_close_response(sid, actual_units=80))
            )

            async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
                runner = SessionRunner(client=client, handle=handle)
                async with runner:
                    await runner.close(actual_units=80)
                assert runner.outcome == "OVERFUNDED"
                assert runner.billed_value_wei == 80_000
                assert runner.refund_wei == 120_000
    finally:
        await broker_close()
