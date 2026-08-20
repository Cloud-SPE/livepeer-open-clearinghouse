"""Unit tests for the dataclass <-> proto mapping used by GrpcPaymentDaemonClient.

These exercise only the pure encoding helpers; nothing here talks to a
real daemon. The full gRPC integration is covered separately when
``PAYMENT_DAEMON_MODE=grpc`` is enabled in a live stack.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    FundingIntent,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.payment_daemon.client import (
    biguint_bytes_to_decimal,
    dataclass_request_to_proto,
    int_to_biguint_bytes,
    proto_response_to_dataclass,
)


@pytest.mark.unit
def test_biguint_zero_is_empty_bytes() -> None:
    assert int_to_biguint_bytes(0) == b""
    assert biguint_bytes_to_decimal(b"") == Decimal(0)


@pytest.mark.unit
def test_biguint_round_trip_small() -> None:
    assert biguint_bytes_to_decimal(int_to_biguint_bytes(1)) == Decimal(1)
    assert biguint_bytes_to_decimal(int_to_biguint_bytes(255)) == Decimal(255)
    assert biguint_bytes_to_decimal(int_to_biguint_bytes(1_000)) == Decimal(1_000)


@pytest.mark.unit
def test_biguint_round_trip_max_uint256() -> None:
    n = 2**256 - 1
    encoded = int_to_biguint_bytes(n)
    assert len(encoded) == 32
    assert biguint_bytes_to_decimal(encoded) == Decimal(n)


@pytest.mark.unit
def test_biguint_rejects_negative() -> None:
    with pytest.raises(ValueError):
        int_to_biguint_bytes(-1)


@pytest.mark.unit
def test_biguint_accepts_decimal_unsigned() -> None:
    assert int_to_biguint_bytes(Decimal(42)) == (42).to_bytes(1, "big")


def _sample_request(funded_wei: int = 200_000) -> CreatePaymentRequest:
    return CreatePaymentRequest(
        mint_request_id="loc:test-mint-1",
        recipient=bytes.fromhex("11" * 20),
        ticket_params_base_url="https://orch.example/livepeer",
        accepted_price=AcceptedPrice(
            capability="openai:chat-completions",
            offering="gpt-oss-20b",
            price_per_unit_wei=Decimal("1000"),
            units_per_price=1,
            work_unit_name="token",
            quote_ref=QuoteRef(
                quote_id="q-1",
                quote_version=2,
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
def test_request_to_proto_carries_every_field() -> None:
    req = _sample_request()
    proto = dataclass_request_to_proto(req)
    assert proto.mint_request_id == "loc:test-mint-1"
    assert proto.recipient == req.recipient
    assert proto.ticket_params_base_url == req.ticket_params_base_url

    ap = proto.accepted_price
    assert ap.capability == "openai:chat-completions"
    assert ap.offering == "gpt-oss-20b"
    assert ap.units_per_price == 1
    assert ap.work_unit_name == "token"
    assert biguint_bytes_to_decimal(bytes(ap.price_per_unit_wei.value)) == Decimal(1000)

    qr = ap.quote_ref
    assert qr.quote_id == "q-1"
    assert qr.quote_version == 2
    assert bytes(qr.constraint_fingerprint) == b"\x00" * 32
    assert bytes(qr.route_fingerprint) == b"\x11" * 32

    f = proto.funding
    assert f.estimated_units == 200
    assert f.max_total_units == 200
    assert biguint_bytes_to_decimal(bytes(f.funded_value_wei.value)) == Decimal(200_000)
    assert f.top_up_allowed is False


@pytest.mark.unit
def test_proto_response_to_dataclass() -> None:
    # Build a fake response by going dataclass -> proto so we exercise the
    # decoder against a real generated message instance.
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    proto = payer_daemon_pb2.CreatePaymentResponse(
        payment_bytes=b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT-V1-test",
        tickets_created=1,
        expected_value=types_pb2.BigUInt(value=int_to_biguint_bytes(12_345)),
        funded_value_wei=types_pb2.BigUInt(value=int_to_biguint_bytes(50_000)),
        accepted_quote_ref=types_pb2.QuoteRef(
            quote_id="q-2",
            quote_version=3,
            constraint_fingerprint=b"\x22" * 32,
            route_fingerprint=b"\x33" * 32,
        ),
        work_id="deadbeef" * 8,
    )

    dc = proto_response_to_dataclass(proto)
    assert dc.payment_bytes == b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT-V1-test"
    assert dc.tickets_created == 1
    assert dc.expected_value == Decimal(12_345)
    assert dc.funded_value_wei == Decimal(50_000)
    assert dc.accepted_quote_ref.quote_id == "q-2"
    assert dc.accepted_quote_ref.quote_version == 3
    assert dc.work_id == "deadbeef" * 8
    # base64 form of payment_bytes is URL-safe; smoke-test the property
    assert isinstance(dc.payment_bytes_b64, str)
