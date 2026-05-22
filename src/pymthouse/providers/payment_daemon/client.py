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
# Grpc implementation — stub, deferred
# ---------------------------------------------------------------------------


class GrpcPaymentDaemonClient:
    """Real gRPC client over a Unix socket.

    Not yet implemented. The required steps:
        1. Run ``make protoc`` to generate stubs into
           ``providers/payment_daemon/_gen/``.
        2. Wire ``grpc.aio.insecure_channel("unix:" + socket_path)``.
        3. Map dataclass requests to the generated protobuf messages and
           back.

    Until then, swap in ``MockPaymentDaemonClient`` via
    ``pymthouse/dependencies.py`` for dev / tests.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    async def health(self) -> bool:
        raise NotImplementedError("GrpcPaymentDaemonClient pending — run `make protoc`")

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        raise NotImplementedError("GrpcPaymentDaemonClient pending — run `make protoc`")
