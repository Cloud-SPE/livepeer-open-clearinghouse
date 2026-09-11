"""Unit tests for the dataclass <-> proto mapping used by GrpcPaymentDaemonClient.

These exercise only the pure encoding helpers; nothing here talks to a
real daemon. The full gRPC integration is covered separately when
``PAYMENT_DAEMON_MODE=grpc`` is enabled in a live stack.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    AccountFundingIntent,
    CreatePaymentRequest,
    CreateSpendAuthorizationRequest,
    FundingIntent,
    GrpcPaymentDaemonClient,
    MintOutcomeUnknown,
    MockPaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.payment_daemon.client import (
    biguint_bytes_to_decimal,
    dataclass_request_to_proto,
    int_to_biguint_bytes,
    proto_response_to_dataclass,
    spend_authorization_request_to_proto,
    validate_funding_response,
)


def _sample_authorization_request() -> CreateSpendAuthorizationRequest:
    return CreateSpendAuthorizationRequest(
        payee=bytes.fromhex("22" * 20),
        authorization_id="auth-1",
        request_id="request-1",
        session_id="",
        protocol="paid-job/v1",
        accepted_price=_sample_request().accepted_price,
        max_debit_wei=Decimal(200_000),
        max_total_units=200,
        not_before=datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        expires_at=datetime(2026, 9, 9, 12, 5, tzinfo=UTC),
        request_digest=b"\x33" * 32,
        caller_public_key=b"",
        revision=0,
        predecessor_authorization_id="",
        broker_uri="https://broker.example",
        chain_id=42161,
    )


@pytest.mark.unit
def test_spend_authorization_request_maps_exact_contract() -> None:
    proto = spend_authorization_request_to_proto(_sample_authorization_request())
    assert proto.payee == bytes.fromhex("22" * 20)
    assert proto.authorization_id == "auth-1"
    assert proto.request_id == "request-1"
    assert proto.session_id == ""
    assert proto.protocol == "paid-job/v1"
    assert proto.accepted_price.capability == "openai:chat-completions"
    assert proto.max_debit_wei.value == int_to_biguint_bytes(200_000)
    assert proto.max_total_units == 200
    assert proto.not_before == "2026-09-09T12:00:00Z"
    assert proto.expires_at == "2026-09-09T12:05:00Z"
    assert proto.request_digest == b"\x33" * 32
    assert proto.broker_uri == "https://broker.example"
    assert proto.chain_id == 42161
    assert proto.denomination == "wei"


@pytest.mark.unit
def test_spend_authorization_timestamps_use_go_rfc3339nano_canonical_form() -> None:
    request = _sample_authorization_request()
    proto = spend_authorization_request_to_proto(
        replace(
            request,
            not_before=datetime(2026, 9, 9, 12, 0, 0, 120000, tzinfo=UTC),
            expires_at=datetime(2026, 9, 9, 12, 5, 0, 123400, tzinfo=UTC),
        )
    )

    assert proto.not_before == "2026-09-09T12:00:00.12Z"
    assert proto.expires_at == "2026-09-09T12:05:00.1234Z"


@pytest.mark.unit
def test_spend_authorization_request_rejects_invalid_scope() -> None:
    from dataclasses import replace

    request = _sample_authorization_request()
    with pytest.raises(ValueError, match="20 bytes"):
        spend_authorization_request_to_proto(replace(request, payee=b"short"))
    with pytest.raises(ValueError, match="after not_before"):
        spend_authorization_request_to_proto(replace(request, expires_at=request.not_before))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grpc_create_spend_authorization_maps_and_checks_identity() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2

    request = _sample_authorization_request()
    signed = await MockPaymentDaemonClient().create_spend_authorization(request)

    class Stub:
        request: object | None = None

        async def CreateSpendAuthorization(self, request: object) -> object:
            self.request = request
            return payer_daemon_pb2.CreateSpendAuthorizationResponse(
                authorization_bytes=signed.authorization_bytes,
                authorization_id="auth-1",
                payer=b"\xaa" * 20,
            )

    stub = Stub()
    client = GrpcPaymentDaemonClient("/unused")
    client._stub = stub
    response = await client.create_spend_authorization(request)
    assert stub.request is not None
    assert response.authorization_bytes == signed.authorization_bytes
    assert response.authorization_id == "auth-1"
    assert response.payer == b"\xaa" * 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grpc_create_spend_authorization_rejects_changed_identity() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2

    class Stub:
        async def CreateSpendAuthorization(self, _request: object) -> object:
            return payer_daemon_pb2.CreateSpendAuthorizationResponse(
                authorization_bytes=b"signed-wire",
                authorization_id="other-auth",
                payer=b"\xaa" * 20,
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    with pytest.raises(PaymentDaemonError, match="different authorization_id"):
        await client.create_spend_authorization(_sample_authorization_request())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grpc_create_spend_authorization_rejects_scope_drift() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    request = _sample_authorization_request()
    signed_response = await MockPaymentDaemonClient().create_spend_authorization(request)
    signed = types_pb2.SpendAuthorization.FromString(signed_response.authorization_bytes)
    signed.payload.broker_uri = "https://different.example"

    class Stub:
        async def CreateSpendAuthorization(self, _request: object) -> object:
            return payer_daemon_pb2.CreateSpendAuthorizationResponse(
                authorization_bytes=signed.SerializeToString(deterministic=True),
                authorization_id="auth-1",
                payer=b"\xaa" * 20,
            )

    client = GrpcPaymentDaemonClient("/unused")
    client._stub = Stub()
    with pytest.raises(PaymentDaemonError, match=r"changed.*scope"):
        await client.create_spend_authorization(request)


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
def test_request_to_proto_carries_shared_account_snapshot() -> None:
    req = replace(
        _sample_request(),
        account_funding=AccountFundingIntent(
            target_available_wei=Decimal(75_000),
            observed_available_wei=Decimal(25_000),
        ),
    )
    proto = dataclass_request_to_proto(req)
    assert biguint_bytes_to_decimal(
        bytes(proto.account_funding.target_available_wei.value)
    ) == Decimal(75_000)
    assert biguint_bytes_to_decimal(
        bytes(proto.account_funding.observed_available_wei.value)
    ) == Decimal(25_000)


@pytest.mark.unit
def test_zero_shortfall_response_is_valid_without_payment_envelope() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    request = replace(
        _sample_request(),
        account_funding=AccountFundingIntent(
            target_available_wei=Decimal(50_000),
            observed_available_wei=Decimal(50_000),
        ),
    )
    proto = payer_daemon_pb2.CreatePaymentResponse(
        expected_value=types_pb2.BigUInt(),
        funded_value_wei=types_pb2.BigUInt(),
        account_shortfall_wei=types_pb2.BigUInt(),
        accepted_quote_ref=types_pb2.QuoteRef(quote_id="q-1", quote_version=2),
    )
    response = proto_response_to_dataclass(proto)
    assert response.payment_bytes == b""
    assert response.sender == b""
    assert validate_funding_response(request, response) is response


@pytest.mark.unit
def test_account_funding_rejects_daemon_shortfall_drift() -> None:
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    request = replace(
        _sample_request(),
        account_funding=AccountFundingIntent(
            target_available_wei=Decimal(75_000),
            observed_available_wei=Decimal(25_000),
        ),
    )
    proto = payer_daemon_pb2.CreatePaymentResponse(
        payment_bytes=types_pb2.Payment(sender=b"\xbb" * 20).SerializeToString(),
        tickets_created=1,
        expected_value=types_pb2.BigUInt(value=int_to_biguint_bytes(40_000)),
        funded_value_wei=types_pb2.BigUInt(value=int_to_biguint_bytes(40_000)),
        account_shortfall_wei=types_pb2.BigUInt(value=int_to_biguint_bytes(40_000)),
        accepted_quote_ref=types_pb2.QuoteRef(),
        creation_round=700,
        expires_after_round=702,
        ticket_validity_period=3,
        ticket_validity_period_observed_at="2026-08-21T12:00:00Z",
    )
    with pytest.raises(PaymentDaemonError, match="bounded shortfall"):
        validate_funding_response(request, proto_response_to_dataclass(proto))


@pytest.mark.unit
def test_proto_response_to_dataclass() -> None:
    # Build a fake response by going dataclass -> proto so we exercise the
    # decoder against a real generated message instance.
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    proto = payer_daemon_pb2.CreatePaymentResponse(
        payment_bytes=types_pb2.Payment(sender=b"\xbb" * 20).SerializeToString(),
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
        predecessor_work_id="cafebabe" * 8,
        creation_round=700,
        expires_after_round=702,
        ticket_validity_period=3,
        ticket_validity_period_observed_at="2026-08-21T12:00:00Z",
    )

    dc = proto_response_to_dataclass(proto)
    assert dc.sender == b"\xbb" * 20
    assert dc.tickets_created == 1
    assert dc.expected_value == Decimal(12_345)
    assert dc.funded_value_wei == Decimal(50_000)
    assert dc.accepted_quote_ref.quote_id == "q-2"
    assert dc.accepted_quote_ref.quote_version == 3
    assert dc.work_id == "deadbeef" * 8
    assert dc.predecessor_work_id == "cafebabe" * 8
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
        payment_bytes=types_pb2.Payment(sender=b"\xbb" * 20).SerializeToString(),
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
def test_funding_response_rejects_self_referential_predecessor() -> None:
    request = _sample_request(funded_wei=50_000)
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2

    proto = payer_daemon_pb2.CreatePaymentResponse(
        payment_bytes=types_pb2.Payment(sender=b"\xbb" * 20).SerializeToString(),
        expected_value=types_pb2.BigUInt(value=int_to_biguint_bytes(50_000)),
        funded_value_wei=types_pb2.BigUInt(value=int_to_biguint_bytes(50_000)),
        accepted_quote_ref=types_pb2.QuoteRef(),
        work_id="ab" * 32,
        predecessor_work_id="ab" * 32,
        creation_round=700,
        expires_after_round=702,
        ticket_validity_period=3,
        ticket_validity_period_observed_at="2026-08-21T12:00:00Z",
    )

    with pytest.raises(PaymentDaemonError, match="self-referential"):
        validate_funding_response(request, proto_response_to_dataclass(proto))


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
