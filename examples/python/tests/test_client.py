"""Tests stub PymtHouse's HTTP surface via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from pymthouse_sdk import (
    InsufficientCredit,
    Mint,
    NoRouteAvailable,
    PymtHouseClient,
    PymtHouseError,
    RateLimited,
)

BASE = "http://test.local"
KEY = "pymth_live_test_key_value"


@respx.mock
async def test_mint_payment_happy_path() -> None:
    respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(
            201,
            json={
                "payment_id": "00000000-0000-0000-0000-000000000001",
                "work_id": "deadbeefdeadbeef",
                "payment_bytes": "AAAA",
                "expected_value_wei": "244140",
                "funded_value_wei": "25000000000",
                "recipient_eth_address": "0xd003",
            },
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        mint = await ph.mint_payment(
            capability="openai:chat-completions",
            offering="vllm-qwen3.6-27b-default",
            work_units=1000,
        )
    assert isinstance(mint, Mint)
    assert mint.payment_bytes == "AAAA"
    assert int(mint.expected_value_wei) == 244140
    assert mint.recipient_eth_address == "0xd003"


@respx.mock
async def test_insufficient_credit_maps_to_typed_error() -> None:
    respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(
            402,
            json={
                "error": {
                    "code": "INSUFFICIENT_CREDIT",
                    "message": "Available 0 < required 1000",
                    "details": {"available_wei": "0", "required_wei": "1000"},
                }
            },
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        with pytest.raises(InsufficientCredit) as exc:
            await ph.mint_payment(capability="x", offering="y", work_units=1)
    assert exc.value.code == "INSUFFICIENT_CREDIT"
    assert exc.value.status == 402
    assert exc.value.details["required_wei"] == "1000"


@respx.mock
async def test_no_route_available() -> None:
    respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(
            404,
            json={"error": {"code": "NO_ROUTE_AVAILABLE", "message": "no route"}},
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        with pytest.raises(NoRouteAvailable):
            await ph.mint_payment(capability="x", offering="y", work_units=1)


@respx.mock
async def test_rate_limited_carries_retry_after() -> None:
    respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"detail": "rate_limited"},
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        with pytest.raises(RateLimited) as exc:
            await ph.mint_payment(capability="x", offering="y", work_units=1)
    assert exc.value.retry_after_seconds == 12


@respx.mock
async def test_idempotency_key_threaded() -> None:
    route = respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(
            201,
            json={
                "payment_id": "00000000-0000-0000-0000-000000000001",
                "work_id": "x",
                "payment_bytes": "AAAA",
                "expected_value_wei": "1",
                "funded_value_wei": "1",
                "recipient_eth_address": "0xd003",
            },
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        await ph.mint_payment(
            capability="x",
            offering="y",
            work_units=1,
            idempotency_key="abc-123",
        )
    assert route.calls.last.request.headers["Idempotency-Key"] == "abc-123"


def test_constructor_rejects_obviously_wrong_key() -> None:
    with pytest.raises(ValueError, match="pymth_"):
        PymtHouseClient(base_url=BASE, api_key="not-a-real-key")


@respx.mock
async def test_list_capabilities_unwraps_items() -> None:
    respx.get(f"{BASE}/v1/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"name": "openai:chat-completions", "work_unit": "tokens", "offerings": []},
                ]
            },
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        caps = await ph.list_capabilities()
    assert len(caps) == 1
    assert caps[0]["name"] == "openai:chat-completions"


@respx.mock
async def test_list_orchestrators_passes_capability_filter() -> None:
    route = respx.get(f"{BASE}/v1/orchestrators").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        await ph.list_orchestrators(capability="openai:chat-completions")
    assert route.calls.last.request.url.params["capability"] == "openai:chat-completions"


@respx.mock
async def test_report_usage_returns_reconciliation() -> None:
    respx.post(f"{BASE}/v1/usage/report").mock(
        return_value=httpx.Response(
            200,
            json={
                "refunded_wei": "12345",
                "payment_status": "settled",
                "new_balance_wei": "999999",
                "usage": {"id": "u1", "actual_work_units": 800, "final_charge_wei": "20000"},
            },
        )
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        result = await ph.report_usage(
            payment_id="00000000-0000-0000-0000-000000000001",
            actual_work_units=800,
            idempotency_key="abc-123",
        )
    assert result["refunded_wei"] == "12345"
    assert result["new_balance_wei"] == "999999"


@respx.mock
async def test_unwrap_handles_non_json_error_body() -> None:
    """Some upstreams (or a bad gateway) return text/plain on 5xx."""
    respx.post(f"{BASE}/v1/payments/mint").mock(
        return_value=httpx.Response(503, text="upstream down"),
    )
    async with PymtHouseClient(base_url=BASE, api_key=KEY) as ph:
        with pytest.raises(PymtHouseError) as exc:
            await ph.mint_payment(capability="x", offering="y", work_units=1)
    assert exc.value.status == 503
