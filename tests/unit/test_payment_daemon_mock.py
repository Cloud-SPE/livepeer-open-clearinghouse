"""Unit tests for MockPaymentDaemonClient."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    AccountFundingIntent,
    CreatePaymentRequest,
    CreateSpendAuthorizationRequest,
    FundingIntent,
    MockPaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
    validate_funding_response,
)


def _request(
    funded_wei: int = 100_000, *, mint_request_id: str = "loc:test-mint"
) -> CreatePaymentRequest:
    return CreatePaymentRequest(
        mint_request_id=mint_request_id,
        recipient=bytes.fromhex("11" * 20),
        ticket_params_base_url="https://orch.example/livepeer",
        accepted_price=AcceptedPrice(
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            price_per_unit_wei=Decimal("1000"),
            units_per_price=1,
            work_unit_name="token",
            quote_ref=QuoteRef(
                quote_id="quote-xyz",
                quote_version=1,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x11" * 32,
            ),
        ),
        funding=FundingIntent(
            funded_value_wei=Decimal(funded_wei),
            estimated_units=funded_wei // 1000,
            max_total_units=funded_wei // 1000,
        ),
    )


@pytest.mark.unit
async def test_create_payment_returns_expected_value_proportional_to_funding() -> None:
    client = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    res = await client.create_payment(_request(50_000))
    assert res.expected_value == Decimal(50_000)
    assert res.funded_value_wei == Decimal(50_000)
    assert res.tickets_created == 1


@pytest.mark.unit
async def test_create_payment_mints_only_shared_account_shortfall() -> None:
    client = MockPaymentDaemonClient()
    request = replace(
        _request(100_000),
        account_funding=AccountFundingIntent(
            target_available_wei=Decimal(100_000),
            observed_available_wei=Decimal(75_000),
        ),
    )
    response = await client.create_payment(request)
    assert response.funded_value_wei == Decimal(25_000)
    assert response.account_shortfall_wei == Decimal(25_000)
    assert validate_funding_response(request, response) is response

    with pytest.raises(PaymentDaemonError, match="does not equal"):
        validate_funding_response(
            request,
            replace(response, expected_value=response.expected_value + 1),
        )


@pytest.mark.unit
async def test_create_payment_returns_no_envelope_for_zero_shortfall() -> None:
    client = MockPaymentDaemonClient()
    request = replace(
        _request(100_000),
        account_funding=AccountFundingIntent(
            target_available_wei=Decimal(100_000),
            observed_available_wei=Decimal(100_000),
        ),
    )
    response = await client.create_payment(request)
    assert response.payment_bytes == b""
    assert response.sender == b""
    assert validate_funding_response(request, response) is response


@pytest.mark.unit
async def test_spend_authorization_is_structural_and_idempotent() -> None:
    client = MockPaymentDaemonClient()
    now = datetime(2026, 9, 9, tzinfo=UTC)
    request = CreateSpendAuthorizationRequest(
        payee=bytes.fromhex("22" * 20),
        authorization_id="auth-1",
        request_id="request-1",
        session_id="",
        protocol="paid-job/v1",
        accepted_price=_request().accepted_price,
        max_debit_wei=Decimal(50_000),
        max_total_units=50,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        request_digest=b"\x33" * 32,
        caller_public_key=b"",
        revision=0,
        predecessor_authorization_id="",
        broker_uri="https://broker.example",
        chain_id=42161,
    )

    first = await client.create_spend_authorization(request)
    replay = await client.create_spend_authorization(request)
    assert replay == first
    assert first.authorization_id == "auth-1"
    assert first.payer == b"\xaa" * 20

    from livepeer.payments.v1 import types_pb2

    wire = types_pb2.SpendAuthorization.FromString(first.authorization_bytes)
    assert wire.payload.domain == "livepeer-spend-authorization/v1"
    assert wire.payload.request_digest == b"\x33" * 32
    with pytest.raises(PaymentDaemonError, match="different request content"):
        await client.create_spend_authorization(replace(request, request_digest=b"\x44" * 32))


@pytest.mark.unit
async def test_payment_bytes_has_recognizable_magic() -> None:
    client = MockPaymentDaemonClient()
    res = await client.create_payment(_request())
    assert res.payment_bytes.startswith(b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT-V1")


@pytest.mark.unit
async def test_work_id_is_hex_and_stable_for_one_payment_session() -> None:
    client = MockPaymentDaemonClient()
    a = await client.create_payment(_request(mint_request_id="loc:a"))
    b = await client.create_payment(_request(mint_request_id="loc:b"))
    # The payer caches one recipient rand for the stable session tuple.
    assert a.work_id == b.work_id
    # 64 hex chars = sha256
    assert len(a.work_id) == 64
    int(a.work_id, 16)  # raises if not hex


@pytest.mark.unit
async def test_identical_mint_request_replays_exact_response() -> None:
    client = MockPaymentDaemonClient()
    request = _request(mint_request_id="loc:replay")
    first = await client.create_payment(request)
    replay = await client.create_payment(request)
    assert replay == first


@pytest.mark.unit
async def test_mint_request_id_reuse_with_different_content_is_refused() -> None:
    client = MockPaymentDaemonClient()
    await client.create_payment(_request(100, mint_request_id="loc:reuse"))
    with pytest.raises(PaymentDaemonError, match="different request content"):
        await client.create_payment(_request(101, mint_request_id="loc:reuse"))


@pytest.mark.unit
async def test_base64_property_is_url_safe_for_headers() -> None:
    client = MockPaymentDaemonClient()
    res = await client.create_payment(_request())
    b64 = res.payment_bytes_b64
    assert isinstance(b64, str)
    # Standard base64 alphabet only (no newlines, no whitespace)
    assert " " not in b64
    assert "\n" not in b64


@pytest.mark.unit
async def test_ev_ratio_below_one() -> None:
    client = MockPaymentDaemonClient(ev_ratio=Decimal("0.5"))
    res = await client.create_payment(_request(100))
    assert res.expected_value == Decimal(50)
    assert res.funded_value_wei == Decimal(100)


@pytest.mark.unit
async def test_funding_response_requires_exact_echo_and_sufficient_ev() -> None:
    request = _request(3_000)
    response = await MockPaymentDaemonClient().create_payment(request)
    validate_funding_response(request, response)

    with pytest.raises(PaymentDaemonError, match="does not echo"):
        validate_funding_response(request, replace(response, funded_value_wei=Decimal(2_999)))
    with pytest.raises(PaymentDaemonError, match="does not cover"):
        validate_funding_response(request, replace(response, expected_value=Decimal(2)))


@pytest.mark.unit
async def test_health_returns_true() -> None:
    client = MockPaymentDaemonClient()
    assert await client.health() is True


@pytest.mark.unit
async def test_get_deposit_info_returns_sensible_shape() -> None:
    client = MockPaymentDaemonClient()
    info = await client.get_deposit_info()
    assert info.deposit_wei > 0
    assert info.reserve_wei >= 0
    assert info.withdraw_round == 0
