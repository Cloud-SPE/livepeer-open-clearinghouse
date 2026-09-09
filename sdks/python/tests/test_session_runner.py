from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import replace

import httpx
import pytest
import respx

from livepeer_open_clearinghouse_sdk import (
    OpenClearinghouseClient,
    SessionAxes,
    SessionBalance,
    SessionHandle,
    SessionRunner,
    WinddownEvent,
)

BASE = "http://loc.test"
BROKER = "http://broker.test"
KEY = "pymth_live_test"


def _handle(*, refill: str = "extensible") -> SessionHandle:
    sid = uuid.UUID("11111111-1111-1111-1111-111111111111")
    return SessionHandle(
        session_id=sid,
        request_id="open-request",
        work_id="wid-sess",
        broker_url=BROKER,
        protocol="paid-session/v1",
        capability="livepeer:test",
        offering="default",
        session=SessionAxes(
            descriptor_schema="livepeer-session-test/v1",
            attachment="external",
            metering="runner-reported",
            refill=refill,
        ),
        session_params={"room": "alpha"},
        payment_envelope="OPEN-ENV",
        expected_value_wei=100_000,
        funded_value_wei=100_000,
        refill_endpoint=f"/v1/sessions/{sid}/refill",
        close_endpoint=f"/v1/sessions/{sid}/close",
    )


def _balance(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "ok",
        "claimed_units": 10,
        "debited_units": 10,
        "unit": "participant_minutes",
        "runway_units": 90,
        "runway_seconds_estimate": 5400,
        "will_refuse_next_refill": False,
    }
    value.update(overrides)
    return value


def _open_response() -> dict[str, object]:
    return {
        "session_id": "broker-session",
        "work_id": "wid-sess",
        "state": "active",
        "runtime": {
            "schema": "livepeer-session-test/v1",
            "public": {"url": "https://runtime.test"},
            "grants": [],
        },
        "credential": "credential",
        "lease": {"expires_at": "2026-08-21T00:00:00Z"},
        "balance": _balance(),
        "control": {
            "status_url": f"{BROKER}/status",
            "topup_url": f"{BROKER}/topup",
            "end_url": f"{BROKER}/end",
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_v1_open_refill_and_close_use_authoritative_http_contract() -> None:
    handle = _handle()
    broker_open = respx.post(f"{BROKER}/v1/session").mock(
        return_value=httpx.Response(200, json=_open_response())
    )
    loc_refill = respx.post(f"{BASE}{handle.refill_endpoint}").mock(
        return_value=httpx.Response(
            200,
            json={
                "request_id": "refill-request",
                "refill_seq": 1,
                "payment_envelope": "REFILL-ENV",
                "expected_value_wei": 50_000,
                "funded_value_wei": 50_000,
                "cap_status": None,
            },
        )
    )
    broker_status = respx.get(f"{BROKER}/status").mock(
        return_value=httpx.Response(200, json={"state": "active", "balance": _balance()})
    )
    broker_topup = respx.post(f"{BROKER}/topup").mock(
        side_effect=[
            httpx.Response(503, json={"error": "retry"}),
            httpx.Response(200, json={"balance": _balance(status="ok")}),
        ]
    )
    broker_end = respx.post(f"{BROKER}/end").mock(
        return_value=httpx.Response(
            204,
            headers={"Livepeer-Settlement": "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0="},
        )
    )
    loc_close = respx.post(f"{BASE}{handle.close_endpoint}").mock(
        return_value=httpx.Response(
            200,
            json={"outcome": "EXACT", "billed_value_wei": 150_000, "refund_wei": 0},
        )
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await SessionRunner(client=client, handle=handle).start()
        runner = SessionRunner(client=client, handle=handle)
        session = await runner.start()  # broker-open replay recovers credential/control
        assert (await runner.status())["state"] == "active"
        low = SessionBalance.from_dict(_balance(status="low", claimed_units=80))
        with pytest.raises(httpx.HTTPStatusError):
            await runner.on_balance(low)
        await runner.on_balance(low)
        result = await runner.close(actual_units=150)

    assert session.runtime_schema == "livepeer-session-test/v1"
    assert result["outcome"] == "EXACT"
    assert broker_open.calls[0].request.headers["Livepeer-Protocol"] == "paid-session/v1"
    assert len(broker_open.calls) == 2
    assert broker_status.called
    assert len(loc_refill.calls) == 1
    assert loc_refill.calls[0].request.headers["Idempotency-Key"]
    assert len(broker_topup.calls) == 2
    assert {call.request.headers["Livepeer-Request-Id"] for call in broker_topup.calls} == {
        "refill-request"
    }
    assert broker_topup.calls[0].request.headers["Livepeer-Request-Id"] == "refill-request"
    assert broker_topup.calls[0].request.headers["Authorization"] == "Bearer credential"
    assert broker_end.called and loc_close.called
    assert json.loads(loc_close.calls[0].request.content)["settlement"] == {
        "payload": {},
        "signature": {},
    }


@pytest.mark.asyncio
@respx.mock
async def test_wholesale_open_sends_authorization_and_exact_committed_body() -> None:
    legacy = _handle()
    exact_body = b'{"gateway_session_id":"bound-id","session_params":{"room":"alpha"}}'
    handle = SessionHandle(
        session_id=legacy.session_id,
        request_id=legacy.request_id,
        work_id=legacy.work_id,
        broker_url=legacy.broker_url,
        protocol=legacy.protocol,
        capability=legacy.capability,
        offering=legacy.offering,
        session=legacy.session,
        session_params=legacy.session_params,
        payment_envelope=None,
        expected_value_wei=legacy.expected_value_wei,
        funded_value_wei=legacy.funded_value_wei,
        refill_endpoint=legacy.refill_endpoint,
        close_endpoint=legacy.close_endpoint,
        spend_authorization="AUTHORIZATION",
        accounting_mode="wholesale_account",
        caller_proof="CALLER-PROOF",
        session_open_body=exact_body,
    )
    broker_open = respx.post(f"{BROKER}/v1/session").mock(
        return_value=httpx.Response(200, json=_open_response())
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        await SessionRunner(client=client, handle=handle).start()

    request = broker_open.calls[0].request
    assert request.content == exact_body
    assert request.headers["Livepeer-Authorization"] == "AUTHORIZATION"
    assert request.headers["Livepeer-Caller-Proof"] == "CALLER-PROOF"
    assert "Livepeer-Payment" not in request.headers


@pytest.mark.asyncio
async def test_wholesale_low_balance_without_cap_approval_winds_down() -> None:
    warnings: list[WinddownEvent] = []
    legacy = _handle()
    handle = replace(
        legacy,
        payment_envelope=None,
        spend_authorization="AUTHORIZATION",
        accounting_mode="wholesale_account",
        caller_proof="PROOF",
        max_total_units=100,
    )
    runner = SessionRunner(
        client=OpenClearinghouseClient(base_url=BASE, api_key=KEY),
        handle=handle,
        on_winddown_warning=warnings.append,
    )
    await runner.on_balance(SessionBalance.from_dict(_balance(status="low")))
    assert warnings == [WinddownEvent("wholesale_cap_extension_required", None)]


@pytest.mark.asyncio
@respx.mock
async def test_wholesale_cap_extension_commits_and_sends_exact_revision() -> None:
    legacy = _handle()
    authorization = base64.b64encode(b"revision-authorization").decode()

    def sign(value: bytes) -> str:
        assert value == b"revision-authorization"
        return "REVISION-PROOF"

    handle = replace(
        legacy,
        payment_envelope=None,
        spend_authorization=base64.b64encode(b"initial").decode(),
        accounting_mode="wholesale_account",
        caller_proof="INITIAL-PROOF",
        max_total_units=100,
        sign_caller_proof=sign,
    )
    respx.post(f"{BROKER}/v1/session").mock(return_value=httpx.Response(200, json=_open_response()))
    loc_refill = respx.post(f"{BASE}{handle.refill_endpoint}").mock(
        return_value=httpx.Response(
            200,
            json={
                "work_id": "loc-auth:revision",
                "request_id": "revision-request",
                "refill_seq": 1,
                "payment_envelope": None,
                "spend_authorization": authorization,
                "accounting_mode": "wholesale_account",
                "expected_value_wei": 0,
                "funded_value_wei": 0,
                "cap_status": None,
            },
        )
    )
    broker_topup = respx.post(f"{BROKER}/topup").mock(
        return_value=httpx.Response(200, json={"balance": _balance(status="ok")})
    )
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        runner = SessionRunner(
            client=client,
            handle=handle,
            approve_cap_extension=lambda _: 200,
        )
        await runner.on_balance(SessionBalance.from_dict(_balance(status="low")))

    request_body = json.loads(loc_refill.calls[0].request.content)
    assert request_body["max_total_units"] == 200
    assert request_body["workload_request_digest"] == hashlib.sha256(b"{}").hexdigest()
    request = broker_topup.calls[0].request
    assert request.content == b"{}"
    assert request.headers["Livepeer-Authorization"] == authorization
    assert request.headers["Livepeer-Caller-Proof"] == "REVISION-PROOF"
    assert "Livepeer-Payment" not in request.headers


@pytest.mark.asyncio
@respx.mock
async def test_bounded_and_refusal_warning_balances_drain_without_refill() -> None:
    warnings: list[WinddownEvent] = []
    respx.post(f"{BROKER}/v1/session").mock(return_value=httpx.Response(200, json=_open_response()))
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        runner = SessionRunner(
            client=client,
            handle=_handle(refill="bounded"),
            on_winddown_warning=warnings.append,
        )
        await runner.start()
        await runner.on_balance(SessionBalance.from_dict(_balance(status="low")))
        await runner.on_balance(SessionBalance.from_dict(_balance(will_refuse_next_refill=True)))

    assert [event.reason for event in warnings] == [
        "bounded_runway_exhausting",
        "broker_will_refuse_next_refill",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_recipient_rotation_remints_with_fresh_identity_and_declared_rebind() -> None:
    warnings: list[WinddownEvent] = []
    handle = _handle()
    respx.post(f"{BROKER}/v1/session").mock(return_value=httpx.Response(200, json=_open_response()))
    loc_refill = respx.post(f"{BASE}{handle.refill_endpoint}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "work_id": "wid-sess",
                    "request_id": "rejected-request",
                    "refill_seq": 1,
                    "payment_envelope": "REJECTED-ENV",
                    "expected_value_wei": 50_000,
                    "funded_value_wei": 50_000,
                    "cap_status": None,
                    "rebind_from": None,
                },
            ),
            httpx.Response(
                200,
                json={
                    "work_id": "wid-successor",
                    "request_id": "successor-request",
                    "refill_seq": 2,
                    "payment_envelope": "SUCCESSOR-ENV",
                    "expected_value_wei": 50_000,
                    "funded_value_wei": 50_000,
                    "cap_status": None,
                    "rebind_from": "wid-sess",
                },
            ),
        ]
    )
    broker_topup = respx.post(f"{BROKER}/topup").mock(
        side_effect=[
            httpx.Response(409, headers={"Livepeer-Error": "recipient_rotated"}),
            httpx.Response(200, json={"balance": _balance()}),
        ]
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        runner = SessionRunner(client=client, handle=handle, on_winddown_warning=warnings.append)
        await runner.start()
        await runner.on_balance(SessionBalance.from_dict(_balance(status="low")))

    assert len(loc_refill.calls) == 2
    replacement_body = loc_refill.calls[1].request.content.decode()
    assert '"rebind_from":"wid-sess"' in replacement_body
    assert '"replaces_request_id":"rejected-request"' in replacement_body
    assert (
        loc_refill.calls[0].request.headers["Idempotency-Key"]
        != loc_refill.calls[1].request.headers["Idempotency-Key"]
    )
    assert len(broker_topup.calls) == 2
    assert "Livepeer-Rebind-From" not in broker_topup.calls[0].request.headers
    assert broker_topup.calls[1].request.headers["Livepeer-Rebind-From"] == "wid-sess"
    assert broker_topup.calls[1].request.headers["Livepeer-Request-Id"] == "successor-request"
    assert runner.broker_session is not None
    assert runner.broker_session.work_id == "wid-successor"
    assert warnings == []


@pytest.mark.asyncio
@respx.mock
async def test_rebind_refused_drains_without_a_second_rotation() -> None:
    warnings: list[WinddownEvent] = []
    handle = _handle()
    respx.post(f"{BROKER}/v1/session").mock(return_value=httpx.Response(200, json=_open_response()))
    loc_refill = respx.post(f"{BASE}{handle.refill_endpoint}").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "work_id": "wid-sess",
                    "request_id": "rejected-request",
                    "payment_envelope": "REJECTED-ENV",
                },
            ),
            httpx.Response(
                200,
                json={
                    "work_id": "wid-successor",
                    "request_id": "successor-request",
                    "payment_envelope": "SUCCESSOR-ENV",
                    "rebind_from": "wid-sess",
                },
            ),
        ]
    )
    broker_topup = respx.post(f"{BROKER}/topup").mock(
        side_effect=[
            httpx.Response(409, headers={"Livepeer-Error": "recipient_rotated"}),
            httpx.Response(409, headers={"Livepeer-Error": "rebind_refused"}),
        ]
    )

    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        runner = SessionRunner(
            client=client,
            handle=handle,
            on_winddown_warning=warnings.append,
        )
        await runner.start()
        await runner.on_balance(SessionBalance.from_dict(_balance(status="low")))

    assert len(loc_refill.calls) == 2
    assert len(broker_topup.calls) == 2
    assert [warning.reason for warning in warnings] == ["payment_unrecoverable"]


@pytest.mark.asyncio
@respx.mock
async def test_descriptor_mismatch_fails_closed() -> None:
    response = _open_response()
    response["runtime"] = {"schema": "wrong/v1", "public": {}, "grants": []}
    respx.post(f"{BROKER}/v1/session").mock(return_value=httpx.Response(200, json=response))
    async with OpenClearinghouseClient(base_url=BASE, api_key=KEY) as client:
        with pytest.raises(Exception, match="descriptor"):
            await SessionRunner(client=client, handle=_handle()).start()
