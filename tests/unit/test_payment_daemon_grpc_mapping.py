"""Unit tests for the dataclass <-> proto mapping used by GrpcPaymentDaemonClient.

These exercise only the pure encoding helpers; nothing here talks to a
real daemon. The full gRPC integration is covered separately when
``PAYMENT_DAEMON_MODE=grpc`` is enabled in a live stack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    FundingIntent,
    GrpcPaymentDaemonClient,
    MintOutcomeUnknown,
    PaymentDaemonError,
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
        creation_round=700,
        expires_after_round=702,
        ticket_validity_period=3,
        ticket_validity_period_observed_at="2026-08-21T12:00:00Z",
    )

    dc = proto_response_to_dataclass(proto)
    assert dc.payment_bytes == b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT-V1-test"
    assert dc.tickets_created == 1
    assert dc.expected_value == Decimal(12_345)
    assert dc.funded_value_wei == Decimal(50_000)
    assert dc.accepted_quote_ref.quote_id == "q-2"
    assert dc.accepted_quote_ref.quote_version == 3
    assert dc.work_id == "deadbeef" * 8
    assert dc.creation_round == 700
    assert dc.expires_after_round == 702
    assert dc.ticket_validity_period == 3
    assert dc.ticket_validity_period_observed_at == datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    # base64 form of payment_bytes is URL-safe; smoke-test the property
    assert isinstance(dc.payment_bytes_b64, str)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expires_after_round", "observed_at"),
    [(701, "2026-08-21T12:00:00Z"), (702, "")],
)
def test_proto_response_rejects_invalid_validity_telemetry(
    expires_after_round: int, observed_at: str
) -> None:
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    proto = payer_daemon_pb2.CreatePaymentResponse(
        expected_value=types_pb2.BigUInt(),
        funded_value_wei=types_pb2.BigUInt(),
        accepted_quote_ref=types_pb2.QuoteRef(),
        creation_round=700,
        expires_after_round=expires_after_round,
        ticket_validity_period=3,
        ticket_validity_period_observed_at=observed_at,
    )
    with pytest.raises(PaymentDaemonError):
        proto_response_to_dataclass(proto)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_deposit_info_maps_validity_telemetry() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2

    class Stub:
        async def GetDepositInfo(self, _request: object) -> object:
            return payer_daemon_pb2.GetDepositInfoResponse(
                deposit=int_to_biguint_bytes(12_345),
                reserve=int_to_biguint_bytes(6_789),
                withdraw_round=4400,
                current_round=4310,
                ticket_validity_period=3,
                ticket_validity_period_observed_at="2026-08-21T12:00:00Z",
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    info = await client.get_deposit_info()
    assert info.deposit_wei == Decimal(12_345)
    assert info.reserve_wei == Decimal(6_789)
    assert info.withdraw_round == 4400
    assert info.current_round == 4310
    assert info.ticket_validity_period == 3
    assert info.ticket_validity_period_observed_at == datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_incomplete_mint_reservation_maps_to_outcome_unknown() -> None:
    import grpc

    class Stub:
        async def CreatePayment(self, _request: object) -> object:
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.FAILED_PRECONDITION,
                grpc.aio.Metadata(),
                grpc.aio.Metadata(),
                details="mint_request_id was reserved but never completed; use a new id",
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    with pytest.raises(MintOutcomeUnknown):
        await client.create_payment(_sample_request())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_recipient_feedback_treats_aborted_as_acknowledgement() -> None:
    import grpc

    class Stub:
        request: object | None = None

        async def ReportPaymentResult(self, request: object) -> object:
            self.request = request
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.ABORTED,
                grpc.aio.Metadata(),
                grpc.aio.Metadata(),
                details="payment session rotated; retry exactly once",
            )

    stub = Stub()
    client = GrpcPaymentDaemonClient("/unused")
    client._stub = stub
    await client.report_invalid_recipient_rand(
        work_id="ab" * 32,
        capability="video:transcode.abr",
        offering="default",
    )

    assert stub.request is not None
    assert stub.request.work_id == "ab" * 32  # type: ignore[attr-defined]
    assert stub.request.capability == "video:transcode.abr"  # type: ignore[attr-defined]
    assert stub.request.offering == "default"  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_recipient_feedback_rejects_non_aborted_failure() -> None:
    import grpc

    class Stub:
        async def ReportPaymentResult(self, _request: object) -> object:
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.UNAVAILABLE,
                grpc.aio.Metadata(),
                grpc.aio.Metadata(),
                details="payer unavailable",
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    with pytest.raises(PaymentDaemonError, match="UNAVAILABLE"):
        await client.report_invalid_recipient_rand(
            work_id="ab" * 32,
            capability="video:transcode.abr",
            offering="default",
        )
