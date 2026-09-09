"""Typed payment-daemon sender boundary for legacy and wholesale modes.

The Protocol mirrors the subset of `payment-daemon`'s sender RPCs that
Livepeer Open Clearinghouse uses. See ``docs/references/payment-daemon.md``.

The legacy payment RPC remains available during migration. Account-aware
routes additionally use the published shortfall intent and spend-authorization
RPCs from the pinned Modules protocol.
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
_ETH_SIGNATURE_BYTES = 65


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
class AccountFundingIntent:
    """Shared payer-payee account snapshot used to compute a bounded shortfall."""

    target_available_wei: Decimal
    observed_available_wei: Decimal


@dataclass(frozen=True, slots=True)
class CreatePaymentRequest:
    """Mirror of `livepeer.payments.v1.CreatePaymentRequest`."""

    mint_request_id: str
    recipient: bytes
    ticket_params_base_url: str
    accepted_price: AcceptedPrice
    funding: FundingIntent
    account_funding: AccountFundingIntent | None = None


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
    ticket_validity_period_observed_at: datetime | None
    predecessor_work_id: str = ""
    account_shortfall_wei: Decimal | None = None

    @property
    def payment_bytes_b64(self) -> str:
        """The header-ready base64 form of payment_bytes."""
        return base64.b64encode(self.payment_bytes).decode("ascii")


@dataclass(frozen=True, slots=True)
class CreateSpendAuthorizationRequest:
    """Typed input to the payer daemon's single-purpose signer."""

    payee: bytes
    authorization_id: str
    request_id: str
    session_id: str
    protocol: str
    accepted_price: AcceptedPrice
    max_debit_wei: Decimal
    max_total_units: int
    not_before: datetime
    expires_at: datetime
    request_digest: bytes
    caller_public_key: bytes
    revision: int
    predecessor_authorization_id: str
    broker_uri: str
    chain_id: int
    denomination: str = "wei"


@dataclass(frozen=True, slots=True)
class CreateSpendAuthorizationResponse:
    """Opaque signed authorization plus its payer and idempotency identity."""

    authorization_bytes: bytes
    authorization_id: str
    payer: bytes

    @property
    def authorization_b64(self) -> str:
        return base64.b64encode(self.authorization_bytes).decode("ascii")


class PaymentDaemonClient(Protocol):
    """The Protocol used by `domains/payments` to mint payments."""

    async def create_payment(self, request: CreatePaymentRequest) -> CreatePaymentResponse: ...

    async def create_spend_authorization(
        self, request: CreateSpendAuthorizationRequest
    ) -> CreateSpendAuthorizationResponse: ...

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
    if request.account_funding is not None:
        requested = max(
            Decimal(0),
            request.account_funding.target_available_wei
            - request.account_funding.observed_available_wei,
        )
        if response.account_shortfall_wei != requested:
            raise PaymentDaemonError(
                "daemon account_shortfall_wei does not match the bounded shortfall"
            )
        if response.expected_value != requested:
            raise PaymentDaemonError(
                "daemon expected_value does not equal the bounded account shortfall"
            )
    if response.funded_value_wei != requested:
        raise PaymentDaemonError(
            "daemon funded_value_wei does not echo the requested funding intent"
        )
    if request.account_funding is None and response.expected_value < requested:
        raise PaymentDaemonError(
            "daemon expected_value does not cover the requested funding intent"
        )
    if request.account_funding is not None and requested == 0:
        if response.payment_bytes or response.tickets_created or response.expected_value:
            raise PaymentDaemonError("daemon minted a payment for a zero account shortfall")
        return response
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
        self._authorization_replays: dict[
            str, tuple[CreateSpendAuthorizationRequest, CreateSpendAuthorizationResponse]
        ] = {}
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
        if request.account_funding is not None:
            funded = max(
                Decimal(0),
                request.account_funding.target_available_wei
                - request.account_funding.observed_available_wei,
            )
        expected_value = (funded * self._ev_ratio).quantize(Decimal(1))

        if funded == 0 and request.account_funding is not None:
            response = CreatePaymentResponse(
                payment_bytes=b"",
                sender=b"",
                tickets_created=0,
                expected_value=Decimal(0),
                funded_value_wei=Decimal(0),
                accepted_quote_ref=request.accepted_price.quote_ref,
                work_id="",
                creation_round=0,
                expires_after_round=0,
                ticket_validity_period=0,
                ticket_validity_period_observed_at=None,
                account_shortfall_wei=Decimal(0),
            )
            self._mint_replays[request.mint_request_id] = (request, response)
            return response

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
            account_shortfall_wei=(funded if request.account_funding is not None else None),
        )
        self._mint_replays[request.mint_request_id] = (request, response)
        return response

    async def create_spend_authorization(
        self, request: CreateSpendAuthorizationRequest
    ) -> CreateSpendAuthorizationResponse:
        """Return a deterministic, structurally valid authorization test envelope."""

        recorded = self._authorization_replays.get(request.authorization_id)
        if recorded is not None:
            original_request, original_response = recorded
            if request != original_request:
                raise PaymentDaemonError("authorization_id was used for different request content")
            return original_response
        proto = spend_authorization_request_to_proto(request)
        from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

        payer = b"\xaa" * _ETH_ADDRESS_BYTES
        payload = _expected_authorization_payload(proto, payer=payer)
        wire = types_pb2.SpendAuthorization(
            payload=payload,
            signature=b"\x00" * _ETH_SIGNATURE_BYTES,
        ).SerializeToString(deterministic=True)
        response = CreateSpendAuthorizationResponse(
            authorization_bytes=wire,
            authorization_id=request.authorization_id,
            payer=payer,
        )
        validate_spend_authorization_response(request, response)
        self._authorization_replays[request.authorization_id] = (request, response)
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


def _format_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("authorization timestamps must include timezone data")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def spend_authorization_request_to_proto(
    request: CreateSpendAuthorizationRequest,
) -> Any:
    """Map the typed LOC authorization intent to the authoritative protobuf."""

    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2  # noqa: PLC0415

    if len(request.payee) != _ETH_ADDRESS_BYTES:
        raise ValueError("authorization payee must be exactly 20 bytes")
    if not request.authorization_id or not request.request_id:
        raise ValueError("authorization_id and request_id are required")
    if request.expires_at <= request.not_before:
        raise ValueError("authorization expires_at must be after not_before")
    if request.protocol not in ("paid-job/v1", "paid-session/v1"):
        raise ValueError("authorization protocol is not supported")
    if request.protocol == "paid-job/v1" and (
        request.session_id or request.revision or request.predecessor_authorization_id
    ):
        raise ValueError("job authorization cannot carry session revision fields")
    if request.protocol == "paid-session/v1" and not request.session_id:
        raise ValueError("session authorization requires session_id")
    if bool(request.revision) != bool(request.predecessor_authorization_id):
        raise ValueError("authorization revision and predecessor must appear together")
    if request.max_debit_wei <= 0 or request.max_total_units <= 0:
        raise ValueError("authorization maximums must be positive")
    if len(request.request_digest) != hashlib.sha256().digest_size:
        raise ValueError("authorization request_digest must be exactly 32 bytes")
    if request.caller_public_key and len(request.caller_public_key) not in (33, 65):
        raise ValueError("authorization caller_public_key must be 33 or 65 bytes")
    if not request.broker_uri.strip():
        raise ValueError("authorization broker_uri is required")
    if request.chain_id <= 0 or request.denomination != "wei":
        raise ValueError("authorization requires a positive chain_id and wei denomination")
    return payer_daemon_pb2.CreateSpendAuthorizationRequest(
        payee=request.payee,
        authorization_id=request.authorization_id,
        request_id=request.request_id,
        session_id=request.session_id,
        protocol=request.protocol,
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
        max_debit_wei=types_pb2.BigUInt(value=int_to_biguint_bytes(request.max_debit_wei)),
        max_total_units=request.max_total_units,
        not_before=_format_rfc3339(request.not_before),
        expires_at=_format_rfc3339(request.expires_at),
        request_digest=request.request_digest,
        caller_public_key=request.caller_public_key,
        revision=request.revision,
        predecessor_authorization_id=request.predecessor_authorization_id,
        broker_uri=request.broker_uri.strip().rstrip("/"),
        chain_id=request.chain_id,
        denomination=request.denomination,
    )


def spend_authorization_response_to_dataclass(
    proto: Any,
) -> CreateSpendAuthorizationResponse:
    response = CreateSpendAuthorizationResponse(
        authorization_bytes=bytes(proto.authorization_bytes),
        authorization_id=str(proto.authorization_id),
        payer=bytes(proto.payer),
    )
    if not response.authorization_bytes:
        raise PaymentDaemonError("daemon returned an empty spend authorization")
    if not response.authorization_id:
        raise PaymentDaemonError("daemon returned an empty authorization_id")
    if len(response.payer) != _ETH_ADDRESS_BYTES:
        raise PaymentDaemonError("daemon authorization payer must be exactly 20 bytes")
    return response


def _expected_authorization_payload(proto: Any, *, payer: bytes) -> Any:
    from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

    return types_pb2.SpendAuthorizationPayload(
        domain="livepeer-spend-authorization/v1",
        payer=payer,
        payee=proto.payee,
        authorization_id=proto.authorization_id,
        request_id=proto.request_id,
        session_id=proto.session_id,
        protocol=proto.protocol,
        capability=proto.accepted_price.capability,
        offering=proto.accepted_price.offering,
        accepted_price=proto.accepted_price,
        max_debit_wei=proto.max_debit_wei,
        max_total_units=proto.max_total_units,
        not_before=proto.not_before,
        expires_at=proto.expires_at,
        request_digest=proto.request_digest,
        caller_public_key=proto.caller_public_key,
        revision=proto.revision,
        predecessor_authorization_id=proto.predecessor_authorization_id,
        broker_uri=proto.broker_uri,
        chain_id=proto.chain_id,
        denomination=proto.denomination,
    )


def validate_spend_authorization_response(
    request: CreateSpendAuthorizationRequest,
    response: CreateSpendAuthorizationResponse,
) -> CreateSpendAuthorizationResponse:
    """Reject a signer response whose opaque envelope changes LOC's scope."""

    from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

    proto_request = spend_authorization_request_to_proto(request)
    try:
        signed = types_pb2.SpendAuthorization.FromString(response.authorization_bytes)
    except (DecodeError, ValueError) as exc:
        raise PaymentDaemonError("daemon returned malformed authorization_bytes") from exc
    if len(signed.signature) != _ETH_SIGNATURE_BYTES:
        raise PaymentDaemonError("daemon returned a malformed authorization signature")
    expected = _expected_authorization_payload(proto_request, payer=response.payer)
    if signed.payload != expected:
        raise PaymentDaemonError("daemon changed the requested authorization scope")
    return response


def dataclass_request_to_proto(request: CreatePaymentRequest):  # type: ignore[no-untyped-def]
    """Map our CreatePaymentRequest dataclass to the generated proto message."""
    # Lazy imports so the runtime image only loads the stubs when grpc mode
    # is actually selected.
    from livepeer.payments.v1 import payer_daemon_pb2, types_pb2  # noqa: PLC0415

    proto = payer_daemon_pb2.CreatePaymentRequest(
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
    if request.account_funding is not None:
        proto.account_funding.CopyFrom(
            types_pb2.AccountFundingIntent(
                target_available_wei=types_pb2.BigUInt(
                    value=int_to_biguint_bytes(request.account_funding.target_available_wei)
                ),
                observed_available_wei=types_pb2.BigUInt(
                    value=int_to_biguint_bytes(request.account_funding.observed_available_wei)
                ),
            )
        )
    return proto


def proto_response_to_dataclass(proto) -> CreatePaymentResponse:  # type: ignore[no-untyped-def]
    """Map a generated CreatePaymentResponse back to our dataclass."""
    from livepeer.payments.v1 import types_pb2  # noqa: PLC0415

    payment_bytes = bytes(proto.payment_bytes)
    sender = b""
    if payment_bytes:
        try:
            payment = types_pb2.Payment.FromString(payment_bytes)
        except (DecodeError, ValueError) as exc:
            raise PaymentDaemonError("daemon returned malformed payment_bytes") from exc
        sender = bytes(payment.sender)
        if len(sender) != _ETH_ADDRESS_BYTES:
            raise PaymentDaemonError("daemon payment_bytes omitted its 20-byte sender")
    observed_at = None
    if proto.ticket_validity_period_observed_at:
        observed_at = _parse_observed_at(proto.ticket_validity_period_observed_at)
    response = CreatePaymentResponse(
        payment_bytes=payment_bytes,
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
        ticket_validity_period_observed_at=observed_at,
        account_shortfall_wei=biguint_bytes_to_decimal(bytes(proto.account_shortfall_wei.value)),
    )
    if response.payment_bytes and (
        response.creation_round <= 0
        or response.ticket_validity_period <= 0
        or response.ticket_validity_period_observed_at is None
        or response.expires_after_round
        != response.creation_round + response.ticket_validity_period - 1
    ):
        raise PaymentDaemonError("daemon returned inconsistent ticket-validity telemetry")
    if not response.payment_bytes and any(
        (
            response.tickets_created,
            response.expected_value,
            response.funded_value_wei,
            response.creation_round,
            response.expires_after_round,
            response.ticket_validity_period,
        )
    ):
        raise PaymentDaemonError("daemon returned funding telemetry without payment_bytes")
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

    async def create_spend_authorization(
        self, request: CreateSpendAuthorizationRequest
    ) -> CreateSpendAuthorizationResponse:
        import grpc  # noqa: PLC0415

        stub = await self._ensure_stub()
        try:
            response = await stub.CreateSpendAuthorization(
                spend_authorization_request_to_proto(request)
            )
        except grpc.aio.AioRpcError as exc:
            raise PaymentDaemonError(
                f"CreateSpendAuthorization {exc.code().name}: {exc.details() or ''}"
            ) from exc
        mapped = spend_authorization_response_to_dataclass(response)
        if mapped.authorization_id != request.authorization_id:
            raise PaymentDaemonError("daemon returned a different authorization_id")
        return validate_spend_authorization_response(request, mapped)
