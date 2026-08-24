"""PaymentDaemonClient Protocol + Mock + Grpc stub.

The Protocol mirrors the subset of `payment-daemon`'s sender RPCs that
Livepeer Open Clearinghouse uses. See ``docs/references/payment-daemon.md``.

Phase 7 ships:
    - PaymentDaemonClient    (Protocol)
    - MockPaymentDaemonClient (working stand-in; deterministic faux payment_bytes)
    - GrpcPaymentDaemonClient (stub; raises NotImplementedError until `make protoc`)

Swap MockPaymentDaemonClient for GrpcPaymentDaemonClient in
``livepeer_open_clearinghouse/dependencies.py`` once stubs are generated.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from google.protobuf.message import DecodeError

# Side-effect import: livepeer_open_clearinghouse._gen injects the generated-stubs dir onto
# sys.path so `from livepeer.payments.v1 import ...` resolves. Loaded
# eagerly so any function in this file can do the absolute `livepeer.*`
# import without first calling _ensure_stub().
from livepeer_open_clearinghouse import _gen  # noqa: F401

_ETH_ADDRESS_BYTES = 20


class PaymentDaemonError(Exception):
    """Generic payment-daemon failure (wraps non-retryable errors)."""

    code = "daemon_error"


class MintOutcomeUnknown(PaymentDaemonError):
    """The payer reserved this mint ID but cannot replay a completed result."""

    code = "mint_outcome_unknown"


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

    mint_request_id: str
    recipient: bytes
    ticket_params_base_url: str
    accepted_price: AcceptedPrice
    funding: FundingIntent


@dataclass(frozen=True, slots=True)
class DepositInfo:
    """Snapshot of the daemon's TicketBroker deposit/reserve state."""

    deposit_wei: Decimal
    reserve_wei: Decimal
    withdraw_round: int
    current_round: int
    ticket_validity_period: int
    ticket_validity_period_observed_at: datetime


@dataclass(frozen=True, slots=True)
class CreatePaymentResponse:
    """Mirror of `livepeer.payments.v1.CreatePaymentResponse`."""

    payment_bytes: bytes
    sender: bytes
    tickets_created: int
    expected_value: Decimal
    funded_value_wei: Decimal
    accepted_quote_ref: QuoteRef
    work_id: str
    creation_round: int
    expires_after_round: int
    ticket_validity_period: int
    ticket_validity_period_observed_at: datetime
    predecessor_work_id: str = ""

    @property
    def payment_bytes_b64(self) -> str:
        """The header-ready base64 form of payment_bytes."""
        return base64.b64encode(self.payment_bytes).decode("ascii")


class PaymentDaemonClient(Protocol):
    """The Protocol used by `domains/payments` to mint payments."""

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse: ...

    async def report_invalid_recipient_rand(
        self, *, work_id: str, capability: str, offering: str
    ) -> None: ...

    async def get_deposit_info(self) -> DepositInfo: ...

    async def health(self) -> bool: ...


def validate_funding_response(
    request: CreatePaymentRequest, response: CreatePaymentResponse
) -> CreatePaymentResponse:
    """Fail closed unless the minted envelope funds the caller's intent."""

    requested = request.funding.funded_value_wei
    if response.funded_value_wei != requested:
        raise PaymentDaemonError(
            "daemon funded_value_wei does not echo the requested funding intent"
        )
    if response.expected_value < requested:
        raise PaymentDaemonError(
            "daemon expected_value does not cover the requested funding intent"
        )
    if len(response.sender) != _ETH_ADDRESS_BYTES:
        raise PaymentDaemonError("daemon payment sender must be exactly 20 bytes")
    if response.predecessor_work_id and response.predecessor_work_id == response.work_id:
        raise PaymentDaemonError("daemon returned a self-referential predecessor_work_id")
    return response


# ---------------------------------------------------------------------------
# Mock implementation — used in Phase 7 and tests
# ---------------------------------------------------------------------------


class MockPaymentDaemonClient:
    """Deterministic-faux payment minting for development and tests.

    Computes EV by simple proportional math instead of probabilistic
    `face_value x win_prob / 2^256` and produces a `payment_bytes` blob
    that's a stable hash of the request (so retries / idempotency tests
    line up). Not wire-compatible with a real orchestrator.
    """

    def __init__(self, ev_ratio: Decimal = Decimal("1.0")) -> None:
        # EV = funded_value * ev_ratio. In a real daemon this is determined
        # by the receiver's faceValue/winProb choice.
        self._ev_ratio = ev_ratio
        self._mint_replays: dict[str, tuple[CreatePaymentRequest, CreatePaymentResponse]] = {}
        self._session_work_ids: dict[tuple[bytes, str, str, str], str] = {}
        self.reported_invalid_recipient_rands: list[tuple[str, str, str]] = []

    async def health(self) -> bool:
        return True

    async def get_deposit_info(self) -> DepositInfo:
        # Pretend the operator funded a 1 ETH float at boot.
        return DepositInfo(
            deposit_wei=Decimal(10**18),
            reserve_wei=Decimal(0),
            withdraw_round=0,
            current_round=100,
            ticket_validity_period=2,
            ticket_validity_period_observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def report_invalid_recipient_rand(
        self, *, work_id: str, capability: str, offering: str
    ) -> None:
        """Record the expected payer-cache eviction in the test double."""
        self.reported_invalid_recipient_rands.append((work_id, capability, offering))
        for key, cached_work_id in tuple(self._session_work_ids.items()):
            if cached_work_id == work_id and key[1:3] == (capability, offering):
                del self._session_work_ids[key]

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
        if not request.mint_request_id:
            raise PaymentDaemonError("mint_request_id is required")
        recorded = self._mint_replays.get(request.mint_request_id)
        if recorded is not None:
            original_request, original_response = recorded
            if request != original_request:
                raise PaymentDaemonError("mint_request_id was used for different request content")
            return original_response

        funded = request.funding.funded_value_wei
        expected_value = (funded * self._ev_ratio).quantize(Decimal(1))

        # work_id = hex(sha256(recipient || quote_id || nonce)) per
        # the daemon's hex-recipient_rand_hash semantics. We synthesize
        # a 32-byte digest from request fields + the mint intent id.
        digest = hashlib.sha256(
            request.recipient
            + request.accepted_price.quote_ref.quote_id.encode("utf-8")
            + request.mint_request_id.encode("utf-8")
        ).digest()
        session_key = (
            request.recipient,
            request.accepted_price.capability,
            request.accepted_price.offering,
            request.ticket_params_base_url,
        )
        work_id = self._session_work_ids.setdefault(session_key, digest.hex())

        # The body of payment_bytes is a stable, recognizable stub: a magic
        # marker + serialized request summary. Not wire-compatible.
        payload = (
            b"OPEN-CLEARINGHOUSE-MOCK-PAYMENT-V1"
            + digest
            + str(funded).encode("utf-8")
            + b"|"
            + request.accepted_price.capability.encode("utf-8")
        )

        response = CreatePaymentResponse(
            payment_bytes=payload,
            sender=b"\xaa" * 20,
            tickets_created=1,
            expected_value=expected_value,
            funded_value_wei=funded,
            accepted_quote_ref=request.accepted_price.quote_ref,
            work_id=work_id,
            creation_round=100,
            expires_after_round=101,
            ticket_validity_period=2,
            ticket_validity_period_observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self._mint_replays[request.mint_request_id] = (request, response)
        return response


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


def _parse_observed_at(value: str) -> datetime:
    """Parse a daemon RFC3339 timestamp and reject missing timezone data."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PaymentDaemonError("daemon returned malformed validity observation time") from exc
    if parsed.tzinfo is None:
        raise PaymentDaemonError("daemon returned timezone-naive validity observation time")
    return parsed


def dataclass_request_to_proto(request: CreatePaymentRequest):  # type: ignore[no-untyped-def]
    """Map our CreatePaymentRequest dataclass to the generated proto message."""
    # Lazy imports so the runtime image only loads the stubs when grpc mode
    # is actually selected.
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2  # noqa: PLC0415

    return payer_daemon_pb2.CreatePaymentRequest(
        mint_request_id=request.mint_request_id,
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
    from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

    try:
        payment = types_pb2.Payment.FromString(bytes(proto.payment_bytes))
    except (DecodeError, ValueError) as exc:
        raise PaymentDaemonError("daemon returned malformed payment_bytes") from exc
    sender = bytes(payment.sender)
    if len(sender) != _ETH_ADDRESS_BYTES:
        raise PaymentDaemonError("daemon payment_bytes omitted its 20-byte sender")
    response = CreatePaymentResponse(
        payment_bytes=bytes(proto.payment_bytes),
        sender=sender,
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
        predecessor_work_id=proto.predecessor_work_id,
        creation_round=int(proto.creation_round),
        expires_after_round=int(proto.expires_after_round),
        ticket_validity_period=int(proto.ticket_validity_period),
        ticket_validity_period_observed_at=_parse_observed_at(
            proto.ticket_validity_period_observed_at
        ),
    )
    if (
        response.creation_round <= 0
        or response.ticket_validity_period <= 0
        or response.expires_after_round
        != response.creation_round + response.ticket_validity_period - 1
    ):
        raise PaymentDaemonError("daemon returned inconsistent ticket-validity telemetry")
    return response


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
        self._channel: Any | None = None
        self._stub: Any | None = None
        self._lock: Any | None = None

    async def _ensure_stub(self) -> Any:
        import asyncio  # noqa: PLC0415

        import grpc.aio  # noqa: PLC0415
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

    async def get_deposit_info(self) -> DepositInfo:
        from livepeer.payments.v1 import payer_daemon_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        resp = await stub.GetDepositInfo(payer_daemon_pb2.GetDepositInfoRequest())
        info = DepositInfo(
            deposit_wei=biguint_bytes_to_decimal(bytes(resp.deposit)),
            reserve_wei=biguint_bytes_to_decimal(bytes(resp.reserve)),
            withdraw_round=int(resp.withdraw_round),
            current_round=int(resp.current_round),
            ticket_validity_period=int(resp.ticket_validity_period),
            ticket_validity_period_observed_at=_parse_observed_at(
                resp.ticket_validity_period_observed_at
            ),
        )
        if info.current_round <= 0 or info.ticket_validity_period <= 0:
            raise PaymentDaemonError("daemon returned invalid current validity telemetry")
        return info

    async def report_invalid_recipient_rand(
        self, *, work_id: str, capability: str, offering: str
    ) -> None:
        """Evict the stale payer session; ABORTED is the expected acknowledgement."""
        import grpc  # noqa: PLC0415
        from livepeer.payments.v1 import payer_daemon_pb2, types_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        try:
            await stub.ReportPaymentResult(
                payer_daemon_pb2.ReportPaymentResultRequest(
                    work_id=work_id,
                    capability=capability,
                    offering=offering,
                    rejection_reason=(types_pb2.PAYMENT_REJECTION_REASON_INVALID_RECIPIENT_RAND),
                )
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.ABORTED:
                return
            raise PaymentDaemonError(
                f"ReportPaymentResult {exc.code().name}: {exc.details() or ''}"
            ) from exc
        raise PaymentDaemonError("ReportPaymentResult did not acknowledge recipient rotation")

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse:
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
            if exc.code() == grpc.StatusCode.FAILED_PRECONDITION and (
                "reserved but never completed" in details or "replay record has expired" in details
            ):
                raise MintOutcomeUnknown(exc.details() or "mint outcome unknown") from exc
            if (
                "deposit" in details
                or "reserve" in details
                or "withdrawround" in details
                or "withdraw_round" in details
            ):
                raise DaemonDepositInsufficient(exc.details() or "deposit insufficient") from exc
            raise PaymentDaemonError(f"{exc.code().name}: {exc.details() or ''}") from exc
        return proto_response_to_dataclass(proto_resp)
