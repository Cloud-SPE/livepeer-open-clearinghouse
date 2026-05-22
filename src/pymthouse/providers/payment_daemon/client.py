"""PaymentDaemonClient Protocol + Mock + Grpc stub.

The Protocol mirrors the subset of `payment-daemon`'s sender RPCs that
PymtHouse uses. See ``docs/references/payment-daemon.md``.

Phase 7 ships:
    - PaymentDaemonClient    (Protocol)
    - MockPaymentDaemonClient (working stand-in; deterministic faux payment_bytes)
    - GrpcPaymentDaemonClient (stub; raises NotImplementedError until `make protoc`)

Swap MockPaymentDaemonClient for GrpcPaymentDaemonClient in
``pymthouse/dependencies.py`` once stubs are generated.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class PaymentDaemonError(Exception):
    """Generic payment-daemon failure (wraps non-retryable errors)."""

    code = "daemon_error"


class DaemonDepositInsufficient(PaymentDaemonError):
    """Sender deposit/reserve is zero or withdraw round is imminent."""

    code = "daemon_deposit_insufficient"


class InvalidRecipientRand(PaymentDaemonError):
    """`ReportPaymentResult` said the cached session is dead. Retry once."""

    code = "invalid_recipient_rand"


@dataclass(frozen=True, slots=True)
class QuoteRef:
    """Mirror of `livepeer.payments.v1.QuoteRef`."""

    quote_id: str
    quote_version: int
    constraint_fingerprint: bytes
    route_fingerprint: bytes


@dataclass(frozen=True, slots=True)
class AcceptedPrice:
    """Mirror of `livepeer.payments.v1.AcceptedPrice`."""

    capability: str
    offering: str
    price_per_unit_wei: Decimal
    units_per_price: int
    work_unit_name: str
    quote_ref: QuoteRef


@dataclass(frozen=True, slots=True)
class FundingIntent:
    """Mirror of `livepeer.payments.v1.FundingIntent`."""

    funded_value_wei: Decimal
    estimated_units: int
    max_total_units: int


@dataclass(frozen=True, slots=True)
class CreatePaymentRequest:
    """Mirror of `livepeer.payments.v1.CreatePaymentRequest`."""

    recipient: bytes
    ticket_params_base_url: str
    accepted_price: AcceptedPrice
    funding: FundingIntent


@dataclass(frozen=True, slots=True)
class CreatePaymentResponse:
    """Mirror of `livepeer.payments.v1.CreatePaymentResponse`."""

    payment_bytes: bytes
    tickets_created: int
    expected_value: Decimal
    funded_value_wei: Decimal
    accepted_quote_ref: QuoteRef
    work_id: str

    @property
    def payment_bytes_b64(self) -> str:
        """The header-ready base64 form of payment_bytes."""
        return base64.b64encode(self.payment_bytes).decode("ascii")


class PaymentDaemonClient(Protocol):
    """The Protocol used by `domains/payments` to mint payments."""

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse: ...

    async def health(self) -> bool: ...


# ---------------------------------------------------------------------------
# Mock implementation — used in Phase 7 and tests
# ---------------------------------------------------------------------------


class MockPaymentDaemonClient:
    """Deterministic-faux payment minting for development and tests.

    Computes EV by simple proportional math instead of probabilistic
    `face_value × win_prob / 2^256` and produces a `payment_bytes` blob
    that's a stable hash of the request (so retries / idempotency tests
    line up). Not wire-compatible with a real orchestrator.
    """

    def __init__(self, ev_ratio: Decimal = Decimal("1.0")) -> None:
        # EV = funded_value * ev_ratio. In a real daemon this is determined
        # by the receiver's faceValue/winProb choice.
        self._ev_ratio = ev_ratio

    async def health(self) -> bool:
        return True

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        funded = request.funding.funded_value_wei
        expected_value = (funded * self._ev_ratio).quantize(Decimal(1))

        # work_id = hex(sha256(recipient || quote_id || nonce)) per
        # the daemon's hex-recipient_rand_hash semantics. We synthesize
        # a 32-byte digest from request fields + a session nonce.
        nonce = secrets.token_bytes(8)
        digest = hashlib.sha256(
            request.recipient
            + request.accepted_price.quote_ref.quote_id.encode("utf-8")
            + nonce
        ).digest()
        work_id = digest.hex()

        # The body of payment_bytes is a stable, recognizable stub: a magic
        # marker + serialized request summary. Not wire-compatible.
        payload = (
            b"PYMTHOUSE-MOCK-PAYMENT-V1"
            + digest
            + str(funded).encode("utf-8")
            + b"|"
            + request.accepted_price.capability.encode("utf-8")
        )

        return CreatePaymentResponse(
            payment_bytes=payload,
            tickets_created=1,
            expected_value=expected_value,
            funded_value_wei=funded,
            accepted_quote_ref=request.accepted_price.quote_ref,
            work_id=work_id,
        )


# ---------------------------------------------------------------------------
# Grpc implementation — real client over Unix domain socket
# ---------------------------------------------------------------------------


def int_to_biguint_bytes(value: int | Decimal) -> bytes:
    """Encode an unsigned integer as the big-endian byte string the daemon expects.

    Per ``livepeer.payments.v1.BigUInt``: zero is the empty byte string
    (canonical form). The shortest big-endian representation is used.
    """
    n = int(value)
    if n < 0:
        raise ValueError(f"BigUInt is unsigned; got {n}")
    if n == 0:
        return b""
    length = (n.bit_length() + 7) // 8
    return n.to_bytes(length, "big")


def biguint_bytes_to_decimal(raw: bytes) -> Decimal:
    """Decode a daemon-returned BigUInt back to Decimal."""
    if not raw:
        return Decimal(0)
    return Decimal(int.from_bytes(raw, "big"))


def dataclass_request_to_proto(request: CreatePaymentRequest):  # type: ignore[no-untyped-def]
    """Map our CreatePaymentRequest dataclass to the generated proto message."""
    # Lazy imports so the runtime image only loads the stubs when grpc mode
    # is actually selected.
    from pymthouse.providers.payment_daemon import _gen  # noqa: F401, PLC0415
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2  # noqa: PLC0415

    return payer_daemon_pb2.CreatePaymentRequest(
        recipient=request.recipient,
        ticket_params_base_url=request.ticket_params_base_url,
        accepted_price=types_pb2.AcceptedPrice(
            price_per_unit_wei=types_pb2.BigUInt(
                value=int_to_biguint_bytes(request.accepted_price.price_per_unit_wei)
            ),
            units_per_price=request.accepted_price.units_per_price,
            work_unit_name=request.accepted_price.work_unit_name,
            capability=request.accepted_price.capability,
            offering=request.accepted_price.offering,
            quote_ref=types_pb2.QuoteRef(
                quote_id=request.accepted_price.quote_ref.quote_id,
                quote_version=request.accepted_price.quote_ref.quote_version,
                constraint_fingerprint=request.accepted_price.quote_ref.constraint_fingerprint,
                route_fingerprint=request.accepted_price.quote_ref.route_fingerprint,
            ),
        ),
        funding=types_pb2.FundingIntent(
            estimated_units=request.funding.estimated_units,
            funded_value_wei=types_pb2.BigUInt(
                value=int_to_biguint_bytes(request.funding.funded_value_wei)
            ),
            max_total_units=request.funding.max_total_units,
            top_up_allowed=False,
        ),
    )


def proto_response_to_dataclass(proto) -> CreatePaymentResponse:  # type: ignore[no-untyped-def]
    """Map a generated CreatePaymentResponse back to our dataclass."""
    return CreatePaymentResponse(
        payment_bytes=bytes(proto.payment_bytes),
        tickets_created=int(proto.tickets_created),
        expected_value=biguint_bytes_to_decimal(bytes(proto.expected_value.value)),
        funded_value_wei=biguint_bytes_to_decimal(bytes(proto.funded_value_wei.value)),
        accepted_quote_ref=QuoteRef(
            quote_id=proto.accepted_quote_ref.quote_id,
            quote_version=int(proto.accepted_quote_ref.quote_version),
            constraint_fingerprint=bytes(proto.accepted_quote_ref.constraint_fingerprint),
            route_fingerprint=bytes(proto.accepted_quote_ref.route_fingerprint),
        ),
        work_id=proto.work_id,
    )


class GrpcPaymentDaemonClient:
    """Async gRPC client for payment-daemon over a Unix domain socket.

    Co-located with the daemon via a shared volume; the daemon doesn't auth
    on the sender RPCs (filesystem-mediated trust), so we connect with
    ``insecure_channel("unix:" + socket_path)``.

    The channel is opened lazily on first call and reused for the life of
    the process. Call :meth:`close` from a shutdown hook to release it
    cleanly.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._channel = None  # type: ignore[assignment]
        self._stub = None  # type: ignore[assignment]
        self._lock = None  # type: ignore[assignment]

    async def _ensure_stub(self):  # type: ignore[no-untyped-def]
        import asyncio  # noqa: PLC0415

        import grpc.aio  # noqa: PLC0415

        from pymthouse.providers.payment_daemon import _gen  # noqa: F401, PLC0415
        from livepeer.payments.v1 import payer_daemon_pb2_grpc  # noqa: PLC0415

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._stub is None:
                self._channel = grpc.aio.insecure_channel(f"unix:{self._socket_path}")
                self._stub = payer_daemon_pb2_grpc.PayerDaemonStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def health(self) -> bool:
        import grpc  # noqa: PLC0415

        from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        try:
            resp = await stub.Health(types_pb2.HealthRequest())
        except grpc.aio.AioRpcError:
            return False
        return getattr(resp, "status", "") == "ok"

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        import grpc  # noqa: PLC0415

        stub = await self._ensure_stub()
        proto_req = dataclass_request_to_proto(request)
        try:
            proto_resp = await stub.CreatePayment(proto_req)
        except grpc.aio.AioRpcError as exc:
            details = (exc.details() or "").lower()
            # The daemon uses Aborted for "session rotated, retry once."
            if exc.code() == grpc.StatusCode.ABORTED:
                raise InvalidRecipientRand(exc.details() or "session rotated") from exc
            if (
                "deposit" in details
                or "reserve" in details
                or "withdrawround" in details
                or "withdraw_round" in details
            ):
                raise DaemonDepositInsufficient(
                    exc.details() or "deposit insufficient"
                ) from exc
            raise PaymentDaemonError(
                f"{exc.code().name}: {exc.details() or ''}"
            ) from exc
        return proto_response_to_dataclass(proto_resp)
