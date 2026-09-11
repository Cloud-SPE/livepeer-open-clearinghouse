"""Business logic for the sessions domain.

PR-3 of exec-plan 002 ships the building blocks:

  - ``create_session`` writes a new ``payment_session`` row in
    ``open`` state.
  - ``get_session`` / ``get_session_by_work_id`` retrieve.
  - ``transition_state`` enforces the lifecycle state machine.
  - ``record_settlement`` appends a ``payment_settlement`` event.
The HTTP handlers compose these operations without re-implementing
the state machine or repo queries.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.types import CustomerPricingSnapshot
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
    SpendAuthorizationGrant,
)
from livepeer_open_clearinghouse.domains.sessions.types import (
    CapStatus,
    CloseSessionResponse,
    CreateSessionResponse,
    PrepareSessionResponse,
    RefillSessionResponse,
    SessionAxesView,
    SessionStatusResponse,
)
from livepeer_open_clearinghouse.domains.telemetry import server_events as telemetry_events
from livepeer_open_clearinghouse.domains.wholesale import service as wholesale_service
from livepeer_open_clearinghouse.domains.wholesale.types import (
    WholesaleFundingLimits,
    WholesaleFundingPlan,
)
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
    NoSettlementDelegation,
    OpenClearinghouseError,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerSettlementClient,
    BrokerSettlementQueryError,
    BrokerWholesaleAccountClient,
    BrokerWholesaleAccountError,
    SpendAuthorizationState,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.payment_daemon import (
    CreateSpendAuthorizationRequest,
    CreateSpendAuthorizationResponse,
    PaymentDaemonClient,
)
from livepeer_open_clearinghouse.providers.registry_daemon import (
    RegistryClient,
    RouteBinding,
    RouteSnapshot,
    SelectedRoute,
    SessionAxes,
    select_bound_route,
)
from livepeer_open_clearinghouse.providers.settlement_verification import (
    SessionSettlementExpectation,
    SettlementVerificationError,
    VerifiedSessionSettlement,
    verify_session_settlement,
)
from livepeer_open_clearinghouse.settings import Settings

# Valid session lifecycle states. Mirrors the docstring on
# ``PaymentSession.state``. Free-form ``str`` in the DB so we can add
# states later without a migration; enforcement happens at this layer.
SESSION_STATE_OPEN = "open"
SESSION_STATE_DRAINING = "draining"
SESSION_STATE_CLOSED = "closed"

SESSION_STATES: frozenset[str] = frozenset(
    {SESSION_STATE_OPEN, SESSION_STATE_DRAINING, SESSION_STATE_CLOSED}
)

# Allowed transitions. ``open → draining → closed`` is the normal
# path; ``open → closed`` is allowed for fast-close (atomic jobs that
# finish synchronously without needing a drain). Reverse transitions
# are never allowed.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SESSION_STATE_OPEN: frozenset({SESSION_STATE_DRAINING, SESSION_STATE_CLOSED}),
    SESSION_STATE_DRAINING: frozenset({SESSION_STATE_CLOSED}),
    SESSION_STATE_CLOSED: frozenset(),  # terminal
}


class SessionsServiceError(Exception):
    """Base for typed errors raised by this module."""


class InvalidSessionState(SessionsServiceError):
    code = "invalid_session_state"


class InvalidSessionTransition(SessionsServiceError):
    code = "invalid_session_transition"


class SessionNotFound(SessionsServiceError):
    code = "session_not_found"


class ProtocolNotSupportedForSession(OpenClearinghouseError):
    """The selected route is not a paid-session/v1 offering."""

    def __init__(self, *, protocol: str) -> None:
        super().__init__(
            code="protocol_not_supported_for_session",
            message=(
                f"protocol {protocol!r} is not accepted by POST /v1/sessions; "
                "paid jobs use POST /v1/jobs"
            ),
            status_code=400,
        )


class InvalidSessionRequest(OpenClearinghouseError):
    """Request validation that pydantic-level constraints can't express."""

    def __init__(self, *, message: str) -> None:
        super().__init__(
            code="invalid_session_request",
            message=message,
            status_code=400,
        )


class RouteBindingMismatch(OpenClearinghouseError):
    """The selected route is no longer an authoritative registry candidate."""

    def __init__(self, *, binding: RouteBinding) -> None:
        super().__init__(
            code="route_binding_mismatch",
            message="the selected route binding is unavailable or has changed",
            status_code=409,
            details={"route_binding": binding.model_dump(mode="json")},
        )


class SessionSettlementVerificationFailed(OpenClearinghouseError):
    """A broker claim cannot authorize session accounting."""

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            code="settlement_verification_failed",
            message="the signed session settlement could not be verified",
            status_code=400,
            details={"reason": reason},
        )


class RefillNotSupported(OpenClearinghouseError):
    """The offering declared a bounded paid session."""

    def __init__(self) -> None:
        super().__init__(
            code="refill_not_supported",
            message=(
                "this offering declares session.refill='bounded'; the session "
                "will end when its funded runway is exhausted"
            ),
            status_code=400,
        )


class SessionNotOpen(OpenClearinghouseError):
    """Refill or other live-session operation attempted on a non-open session."""

    def __init__(self, *, current_state: str) -> None:
        super().__init__(
            code="session_not_open",
            message=f"session is in state {current_state!r}; refill requires 'open'",
            status_code=409,
        )


class SessionCapReached(OpenClearinghouseError):
    """Refill refused because a cap would be crossed."""

    def __init__(self, *, which: str, remaining_wei: int, advice: str) -> None:
        super().__init__(
            code="cap_reached",
            message=f"refill refused: {which} cap reached",
            status_code=402,
            details={
                "which": which,
                "remaining_wei": str(remaining_wei),
                "advice": advice,
            },
        )


PAID_SESSION_PROTOCOL = "paid-session/v1"


_ETH_ADDRESS_HEX_LEN = 40  # 20 bytes hex-encoded


def _bill_value_wei(*, units: int, amount_wei: Decimal, per_units: int) -> Decimal:
    """Apply Modules v2 cumulative billing: ceil(units x amount / per_units)."""

    if per_units < 1:
        raise ValueError("per_units must be positive")
    amount = int(amount_wei)
    return Decimal((units * amount + per_units - 1) // per_units)


def _eth_address_to_bytes(addr: str) -> bytes:
    """Turn a ``0x``-prefixed hex address into the 20 raw bytes the daemon wants.

    Mirrors the helper in ``domains.payments.service`` (duplicated to
    avoid a cross-domain private import; 4 lines isn't worth a shared
    util module yet).
    """
    stripped = addr.removeprefix("0x")
    if len(stripped) != _ETH_ADDRESS_HEX_LEN:
        raise ValueError(f"expected {_ETH_ADDRESS_HEX_LEN}-char hex address, got {addr!r}")
    return bytes.fromhex(stripped)


async def record_spend_authorization_grant(
    db: AsyncSession,
    *,
    engagement_id: uuid.UUID,
    user_id: uuid.UUID,
    route: SelectedRoute,
    request: CreateSpendAuthorizationRequest,
    response: CreateSpendAuthorizationResponse,
) -> SpendAuthorizationGrant:
    """Persist a signed grant without retiring any predecessor implicitly."""

    engagement = await db.scalar(
        select(PaymentSession)
        .where(PaymentSession.id == engagement_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if engagement is None or engagement.user_id != user_id:
        raise SessionNotFound
    if engagement.accounting_mode != "wholesale_account":
        raise InvalidSessionRequest(message="legacy engagement cannot record an authorization")
    if engagement.route_snapshot != route.snapshot():
        raise InvalidSessionRequest(message="authorization route differs from locked engagement")
    if response.authorization_id != request.authorization_id:
        raise InvalidSessionRequest(message="authorization signer changed identity")
    expected_session_id = str(engagement_id) if request.protocol == PAID_SESSION_PROTOCOL else ""
    if request.session_id != expected_session_id:
        raise InvalidSessionRequest(message="authorization session identity changed")
    if request.revision == 0 and engagement.broker_request_id != request.request_id:
        raise InvalidSessionRequest(message="authorization request identity changed")

    existing = await db.scalar(
        select(SpendAuthorizationGrant).where(
            SpendAuthorizationGrant.session_id == engagement_id,
            SpendAuthorizationGrant.revision == request.revision,
        )
    )
    if existing is not None:
        expected = (
            request.authorization_id,
            request.predecessor_authorization_id or None,
            request.request_id,
            request.request_digest.hex(),
            request.caller_public_key.hex(),
            response.authorization_bytes,
            request.protocol,
            response.payer.hex(),
            request.chain_id,
            request.denomination,
            request.max_debit_wei,
            request.max_total_units,
            request.not_before,
            request.expires_at,
        )
        actual = (
            existing.authorization_id,
            existing.predecessor_authorization_id,
            existing.request_id,
            existing.request_digest,
            existing.caller_public_key,
            existing.authorization_bytes,
            existing.protocol,
            existing.payer_eth_address.removeprefix("0x"),
            existing.chain_id,
            existing.denomination,
            existing.max_debit_wei,
            existing.max_total_units,
            _normalized_utc(existing.not_before),
            _normalized_utc(existing.expires_at),
        )
        if actual != expected:
            raise InvalidSessionRequest(message="authorization revision replay changed scope")
        return existing

    if request.revision > 0:
        predecessor = await db.scalar(
            select(SpendAuthorizationGrant).where(
                SpendAuthorizationGrant.session_id == engagement_id,
                SpendAuthorizationGrant.authorization_id == request.predecessor_authorization_id,
            )
        )
        if predecessor is None:
            raise InvalidSessionRequest(message="authorization predecessor is not retained")

    grant = SpendAuthorizationGrant(
        session_id=engagement_id,
        authorization_id=request.authorization_id,
        revision=request.revision,
        predecessor_authorization_id=request.predecessor_authorization_id or None,
        request_id=request.request_id,
        request_digest=request.request_digest.hex(),
        caller_public_key=request.caller_public_key.hex(),
        authorization_bytes=response.authorization_bytes,
        protocol=request.protocol,
        route_snapshot=route.snapshot(),
        payer_eth_address="0x" + response.payer.hex(),
        chain_id=request.chain_id,
        denomination=request.denomination,
        max_debit_wei=request.max_debit_wei,
        max_total_units=request.max_total_units,
        not_before=request.not_before,
        expires_at=request.expires_at,
        state="issued",
        retired_at=None,
    )
    db.add(grant)
    engagement.authorization_id = request.authorization_id
    engagement.work_id = request.authorization_id
    await db.flush()
    return grant


async def mark_spend_authorization_settled(
    db: AsyncSession,
    *,
    engagement_id: uuid.UUID,
    authorization_id: str,
    clock: Clock,
) -> SpendAuthorizationGrant:
    """Record terminal broker-signed use without deleting grant history."""

    grant = await db.scalar(
        select(SpendAuthorizationGrant)
        .where(
            SpendAuthorizationGrant.session_id == engagement_id,
            SpendAuthorizationGrant.authorization_id == authorization_id,
        )
        .with_for_update()
    )
    if grant is None:
        raise SessionSettlementVerificationFailed(reason="missing_authorization")
    if grant.state not in {"issued", "admitted", "settled"}:
        raise SessionSettlementVerificationFailed(reason="authorization_state_conflict")
    grant.state = "settled"
    grant.retired_at = grant.retired_at or clock.now()
    await db.flush()
    return grant


def _normalized_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive UTC persistence for replay checks."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def create_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    work_id: str,
    capability: str,
    offering: str,
    protocol: str,
    route_snapshot: dict[str, Any] | None = None,
    broker_request_id: str | None = None,
    estimated_units: int,
    max_total_units: int,
    funded_value_wei: Decimal,
    clock: Clock,
    sdk_identity: str | None = None,
    accounting_mode: str = "wholesale_account",
    customer_pricing: dict[str, Any] | None = None,
    customer_max_debit_wei: Decimal | None = None,
    session_id: uuid.UUID | None = None,
) -> PaymentSession:
    """Create a new session in the ``open`` state and return it.

    Caller is responsible for any encumbrance accounting on the
    user's balance — this function only writes the session row.
    """
    now = clock.now()
    row = PaymentSession(
        id=session_id or uuid.uuid4(),
        user_id=user_id,
        api_key_id=api_key_id,
        work_id=work_id,
        capability=capability,
        offering=offering,
        protocol=protocol,
        route_snapshot=route_snapshot,
        broker_request_id=broker_request_id,
        state=SESSION_STATE_OPEN,
        estimated_units=estimated_units,
        max_total_units=max_total_units,
        funded_value_wei=funded_value_wei,
        accounting_mode=accounting_mode,
        customer_pricing=customer_pricing,
        customer_max_debit_wei=customer_max_debit_wei,
        opened_at=now,
        sdk_identity=sdk_identity,
    )
    session.add(row)
    await session.flush()
    return row


async def claim_wholesale_engagement(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    route: SelectedRoute,
    broker_request_id: str,
    estimated_units: int,
    max_total_units: int,
    max_debit_wei: Decimal,
    clock: Clock,
    sdk_identity: str | None,
    spend_period_seconds: int,
    spend_period_cap_wei: int,
    gateway_session_id: uuid.UUID | None = None,
) -> PaymentSession:
    """Create and hold one wholesale engagement, or resume its exact replay."""

    if gateway_session_id is not None:
        id_owner = await db.get(PaymentSession, gateway_session_id)
        if id_owner is not None and (
            id_owner.user_id != user_id or id_owner.broker_request_id != broker_request_id
        ):
            raise InvalidSessionRequest(message="gateway_session_id is already in use")
    existing = await db.scalar(
        select(PaymentSession).where(
            PaymentSession.user_id == user_id,
            PaymentSession.broker_request_id == broker_request_id,
            PaymentSession.accounting_mode == "wholesale_account",
        )
    )
    pricing = CustomerPricingSnapshot(
        plan_id="wholesale-pass-through",
        kind="wholesale_pass_through",
        work_unit=route.work_unit,
    ).model_dump(mode="json")
    if existing is not None:
        requested_scope = (
            api_key_id,
            route.capability,
            route.offering,
            route.protocol,
            route.snapshot(),
            estimated_units,
            max_total_units,
            max_debit_wei,
            pricing,
        )
        recorded_scope = (
            existing.api_key_id,
            existing.capability,
            existing.offering,
            existing.protocol,
            existing.route_snapshot,
            existing.estimated_units,
            existing.max_total_units,
            existing.customer_max_debit_wei,
            existing.customer_pricing,
        )
        if recorded_scope != requested_scope:
            raise InvalidSessionRequest(message="wholesale engagement replay changed scope")
        return existing

    engagement = await create_session(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        work_id="",
        capability=route.capability,
        offering=route.offering,
        protocol=route.protocol,
        route_snapshot=route.snapshot(),
        broker_request_id=broker_request_id,
        estimated_units=estimated_units,
        max_total_units=max_total_units,
        funded_value_wei=max_debit_wei,
        clock=clock,
        sdk_identity=sdk_identity,
        accounting_mode="wholesale_account",
        customer_pricing=pricing,
        customer_max_debit_wei=max_debit_wei,
        session_id=gateway_session_id,
    )
    await billing_service.encumber_customer_engagement(
        db,
        user_id=user_id,
        engagement_id=engagement.id,
        amount_wei=max_debit_wei,
        clock=clock,
        period_seconds=spend_period_seconds,
        cap_wei=spend_period_cap_wei,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        winner = await db.scalar(
            select(PaymentSession).where(
                PaymentSession.user_id == user_id,
                PaymentSession.broker_request_id == broker_request_id,
                PaymentSession.accounting_mode == "wholesale_account",
            )
        )
        if winner is None:
            raise
        return await claim_wholesale_engagement(
            db,
            user_id=user_id,
            api_key_id=api_key_id,
            route=route,
            broker_request_id=broker_request_id,
            estimated_units=estimated_units,
            max_total_units=max_total_units,
            max_debit_wei=max_debit_wei,
            clock=clock,
            sdk_identity=sdk_identity,
            spend_period_seconds=spend_period_seconds,
            spend_period_cap_wei=spend_period_cap_wei,
            gateway_session_id=gateway_session_id,
        )
    return engagement


async def get_session(session: AsyncSession, session_id: uuid.UUID) -> PaymentSession | None:
    """Look up by primary key. Returns None if not present."""
    return await session.get(PaymentSession, session_id)


async def get_session_by_work_id(session: AsyncSession, work_id: str) -> PaymentSession | None:
    """Look up by upstream ``work_id`` (the hex recipient_rand_hash).

    Multiple sessions could in principle share a ``work_id`` over time
    if the upstream daemon recycles it; this returns the most recent.
    """
    result = await session.scalars(
        select(PaymentSession)
        .where(PaymentSession.work_id == work_id)
        .order_by(PaymentSession.opened_at.desc())
        .limit(1)
    )
    return result.one_or_none()


async def transition_state(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    from_state: str,
    to_state: str,
    clock: Clock,
) -> PaymentSession:
    """Transition a session from one state to another.

    Raises :class:`InvalidSessionState` if either state name is not in
    :data:`SESSION_STATES`; raises :class:`InvalidSessionTransition`
    if the requested move isn't allowed by the state machine; raises
    :class:`SessionNotFound` if the row doesn't exist; raises
    :class:`InvalidSessionTransition` if the row's current state
    isn't ``from_state`` (optimistic-concurrency guard).

    On success, also sets ``closed_at`` when moving to ``closed``.
    """
    if from_state not in SESSION_STATES or to_state not in SESSION_STATES:
        raise InvalidSessionState
    if to_state not in _ALLOWED_TRANSITIONS[from_state]:
        raise InvalidSessionTransition

    row = await session.get(PaymentSession, session_id)
    if row is None:
        raise SessionNotFound
    if row.state != from_state:
        raise InvalidSessionTransition

    row.state = to_state
    if to_state == SESSION_STATE_CLOSED:
        row.closed_at = clock.now()
    await session.flush()
    return row


async def record_settlement(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    event_type: str,
    clock: Clock,
    actual_units: int | None = None,
    billed_value_wei: Decimal | None = None,
    outcome: str | None = None,
    raw_record: dict[str, Any] | None = None,
) -> PaymentSettlement:
    """Append a ``payment_settlement`` event for ``session_id``.

    ``event_type`` is a free-form string but conventionally one of
    ``refill_granted`` / ``refill_denied`` / ``balance_low`` /
    ``close`` / ``reconcile``.
    """
    row = PaymentSettlement(
        session_id=session_id,
        recorded_at=clock.now(),
        event_type=event_type,
        actual_units=actual_units,
        billed_value_wei=billed_value_wei,
        outcome=outcome,
        raw_record=raw_record,
    )
    session.add(row)
    await session.flush()
    return row


async def mark_polled(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    clock: Clock,
) -> None:
    """Update ``last_polled_at`` for the janitor's poll cadence.

    No-op if the session no longer exists.
    """
    row = await session.get(PaymentSession, session_id)
    if row is None:
        return
    row.last_polled_at = clock.now()
    await session.flush()


# ---------------------------------------------------------------------------
# Session-open orchestration (POST /v1/sessions)
# ---------------------------------------------------------------------------


def _refill_endpoint_for(session_id: uuid.UUID) -> str:
    return f"/v1/sessions/{session_id}/refill"


def _close_endpoint_for(session_id: uuid.UUID) -> str:
    return f"/v1/sessions/{session_id}/close"


_PREPARATION_DOMAIN = b"loc-session-preparation/v1\x00"
_PREPARATION_TTL = timedelta(minutes=5)


def _encode_preparation(payload: dict[str, Any], settings: Settings) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.digest(
        settings.session_secret.get_secret_value().encode(), _PREPARATION_DOMAIN + raw, "sha256"
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + signature.hex()


def _decode_preparation(token: str, settings: Settings) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expected_signature = hmac.digest(
            settings.session_secret.get_secret_value().encode(),
            _PREPARATION_DOMAIN + raw,
            "sha256",
        )
        if not hmac.compare_digest(bytes.fromhex(supplied_signature), expected_signature):
            raise ValueError
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidSessionRequest(message="invalid session preparation token") from exc
    if not isinstance(payload, dict):
        raise InvalidSessionRequest(message="invalid session preparation token")
    return payload


async def prepare_session(
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    capability: str,
    offering: str,
    descriptor_schema: str,
    route_binding: RouteBinding | None,
    registry: RegistryClient,
    clock: Clock,
    settings: Settings,
) -> PrepareSessionResponse:
    """Issue a route-locked session identity before body commitment."""

    route = await select_bound_route(
        registry, capability=capability, offering=offering, binding=route_binding
    )
    if route is None:
        if route_binding is not None:
            raise RouteBindingMismatch(binding=route_binding)
        raise NoRouteAvailable(capability=capability, offering=offering)
    if not route.features.wholesale_accounts:
        raise NoRouteAvailable(capability=capability, offering=offering)
    if route.protocol != PAID_SESSION_PROTOCOL:
        raise ProtocolNotSupportedForSession(protocol=route.protocol)
    if route.session is None or route.session.descriptor_schema != descriptor_schema:
        raise InvalidSessionRequest(message="prepared route descriptor schema changed")
    gateway_session_id = uuid.uuid4()
    expires_at = clock.now() + _PREPARATION_TTL
    binding = route.binding
    payload = {
        "user_id": str(user_id),
        "api_key_id": str(api_key_id),
        "gateway_session_id": str(gateway_session_id),
        "capability": capability,
        "offering": offering,
        "descriptor_schema": descriptor_schema,
        "route_binding": binding.model_dump(mode="json"),
        "expires_at": expires_at.isoformat(),
    }
    return PrepareSessionResponse(
        gateway_session_id=gateway_session_id,
        route_binding=binding,
        broker_url=route.worker_url,
        preparation_token=_encode_preparation(payload, settings),
        expires_at=expires_at,
    )


def _verify_prepared_session(
    *,
    token: str,
    gateway_session_id: uuid.UUID,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    capability: str,
    offering: str,
    descriptor_schema: str,
    route: SelectedRoute,
    clock: Clock,
    settings: Settings,
) -> None:
    payload = _decode_preparation(token, settings)
    expected = {
        "user_id": str(user_id),
        "api_key_id": str(api_key_id),
        "gateway_session_id": str(gateway_session_id),
        "capability": capability,
        "offering": offering,
        "descriptor_schema": descriptor_schema,
        "route_binding": route.binding.model_dump(mode="json"),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise InvalidSessionRequest(message="session preparation scope changed")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise InvalidSessionRequest(message="invalid session preparation expiry") from exc
    if expires_at <= clock.now():
        raise InvalidSessionRequest(message="session preparation expired")


async def _replenish_wholesale_account(
    db: AsyncSession,
    *,
    route: SelectedRoute,
    payer_eth_address: str,
    mint_request_id: str,
    correlation_id: str,
    broker: BrokerWholesaleAccountClient,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
) -> WholesaleFundingPlan:
    """Bring one shared account to target only after its low-water threshold."""

    observation = await broker.get_wholesale_account(
        broker_url=route.worker_url,
        payer_eth_address=payer_eth_address,
        payee_eth_address=route.eth_address,
        chain_id=settings.wholesale_chain_id,
    )
    limits = WholesaleFundingLimits(
        target_available_wei=Decimal(settings.wholesale_target_available_wei),
        replenish_below_wei=Decimal(settings.wholesale_replenish_below_wei),
        max_available_per_payee_wei=Decimal(settings.wholesale_max_available_per_payee_wei),
        max_aggregate_available_wei=Decimal(settings.wholesale_max_aggregate_available_wei),
        max_single_funding_wei=Decimal(settings.wholesale_max_single_funding_wei),
    )
    plan = await wholesale_service.plan_observed_account_shortfall(
        db,
        observation=observation,
        limits=limits,
    )
    funding = await wholesale_service.claim_account_funding(
        db,
        route=route,
        observation=observation,
        plan=plan,
        limits=limits,
        mint_request_id=mint_request_id,
        correlation_id=correlation_id,
        protocol_version="wholesale-account/1.0.0-draft",
    )
    await wholesale_service.complete_account_funding(
        db,
        funding=funding,
        route=route,
        observation=observation,
        plan=plan,
        payer_eth_address=payer_eth_address,
        chain_id=settings.wholesale_chain_id,
        broker=broker,
        daemon=daemon,
        acknowledged_at=clock.now(),
    )
    return plan


async def _open_wholesale_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    route: SelectedRoute,
    session_axes: SessionAxes,
    estimated_runway_units: int,
    max_total_units: int,
    max_debit_wei: Decimal,
    request_id: str,
    request_digest: bytes,
    caller_public_key: bytes,
    broker: BrokerWholesaleAccountClient,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
    sdk_identity: str | None,
    spend_period_seconds: int,
    spend_period_cap_wei: int,
    gateway_session_id: uuid.UUID,
) -> CreateSessionResponse:
    """Create a cumulative-cap session and replenish only shared runway."""

    session_row = await claim_wholesale_engagement(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        broker_request_id=request_id,
        estimated_units=estimated_runway_units,
        max_total_units=max_total_units,
        max_debit_wei=max_debit_wei,
        route=route,
        clock=clock,
        sdk_identity=sdk_identity,
        spend_period_seconds=spend_period_seconds,
        spend_period_cap_wei=spend_period_cap_wei,
        gateway_session_id=gateway_session_id,
    )

    now = clock.now()
    authorization_id = f"loc-auth:{request_id}"
    auth_request, auth_response = await payments_service.issue_route_locked_authorization(
        daemon=daemon,
        route=route,
        authorization_id=authorization_id,
        request_id=request_id,
        session_id=str(session_row.id),
        request_digest=request_digest,
        caller_public_key=caller_public_key,
        max_debit_wei=max_debit_wei,
        max_total_units=max_total_units,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        chain_id=settings.wholesale_chain_id,
    )
    await record_spend_authorization_grant(
        db,
        engagement_id=session_row.id,
        user_id=user_id,
        route=route,
        request=auth_request,
        response=auth_response,
    )
    await db.commit()

    payer = "0x" + auth_response.payer.hex()
    plan = await _replenish_wholesale_account(
        db,
        route=route,
        payer_eth_address=payer,
        mint_request_id=f"loc-account:{request_id}",
        correlation_id=str(session_row.id),
        broker=broker,
        daemon=daemon,
        clock=clock,
        settings=settings,
    )

    return CreateSessionResponse(
        session_id=session_row.id,
        request_id=request_id,
        work_id=authorization_id,
        broker_url=route.worker_url,
        protocol=route.protocol,
        session=SessionAxesView.model_validate(session_axes.model_dump(mode="json")),
        route_snapshot=route.snapshot_view(),
        spend_authorization=auth_response.authorization_b64,
        accounting_mode="wholesale_account",
        expected_value_wei=int(plan.shortfall_wei),
        funded_value_wei=int(plan.shortfall_wei),
        refill_endpoint=_refill_endpoint_for(session_row.id),
        close_endpoint=_close_endpoint_for(session_row.id),
        opened_at=session_row.opened_at,
    )


async def open_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    capability: str,
    offering: str,
    estimated_runway_units: int,
    max_total_units: int,
    sdk_identity: str | None,
    registry: RegistryClient,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
    descriptor_schema: str,
    route_binding: RouteBinding | None = None,
    request_id: str | None = None,
    workload_request_digest: bytes,
    caller_public_key: bytes,
    broker_wholesale: BrokerWholesaleAccountClient | None = None,
    gateway_session_id: uuid.UUID,
    preparation_token: str,
) -> CreateSessionResponse:
    """Open a prepared, route-locked session on the wholesale account.

    The cumulative maximum bounds customer exposure and the signed broker
    authorization. Only bounded account shortfall is ticket-funded.
    """
    broker_request_id = request_id or str(uuid.uuid4())
    if max_total_units < estimated_runway_units:
        raise InvalidSessionRequest(message="max_total_units must be >= estimated_runway_units")

    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # ---- 2. Discovery
    route = await select_bound_route(
        registry,
        capability=capability,
        offering=offering,
        binding=route_binding,
    )
    if route is None:
        if route_binding is not None:
            raise RouteBindingMismatch(binding=route_binding)
        raise NoRouteAvailable(capability=capability, offering=offering)

    # ---- 3. Protocol declaration + validation
    if not route.features.wholesale_accounts:
        raise NoRouteAvailable(capability=capability, offering=offering)
    protocol = route.protocol
    if protocol != PAID_SESSION_PROTOCOL:
        raise ProtocolNotSupportedForSession(protocol=protocol)
    if not route.settlement_keys:
        raise NoSettlementDelegation(capability=capability, offering=offering)
    session_axes = route.session
    if session_axes is None:  # pragma: no cover - SelectedRoute validates this
        raise InvalidSessionRequest(message="session route declaration is unavailable")
    if session_axes.descriptor_schema != descriptor_schema:
        raise InvalidSessionRequest(
            message=(
                f"offering declares descriptor schema {session_axes.descriptor_schema!r}; "
                f"the client requested {descriptor_schema!r}"
            )
        )

    # ---- 4. Cumulative authorization and customer-exposure ceiling
    price_wei = Decimal(route.price_per_work_unit_wei)
    worst_case_value_wei = _bill_value_wei(
        units=max_total_units,
        amount_wei=price_wei,
        per_units=route.units_per_price,
    )

    # Fail before issuing authorization or funding the shared account. The
    # engagement hold later repeats this check transactionally.
    balance = await billing_service.get_balance(db, user_id=user_id)
    if balance.amount_wei < worst_case_value_wei:
        await telemetry_events.emit_mint_refused(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            capability=capability,
            offering=offering,
            which_cap="user_balance",
            remaining_wei=int(balance.amount_wei),
            clock=clock,
        )
        raise InsufficientCredit(
            available_wei=int(balance.amount_wei),
            required_wei=int(worst_case_value_wei),
        )

    _verify_prepared_session(
        token=preparation_token,
        gateway_session_id=gateway_session_id,
        user_id=user_id,
        api_key_id=api_key_id,
        capability=capability,
        offering=offering,
        descriptor_schema=descriptor_schema,
        route=route,
        clock=clock,
        settings=settings,
    )
    if broker_wholesale is None:
        raise DaemonUnavailable(
            daemon="wholesale-account", reason="broker account client is unavailable"
        )
    return await _open_wholesale_session(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        route=route,
        session_axes=session_axes,
        estimated_runway_units=estimated_runway_units,
        max_total_units=max_total_units,
        max_debit_wei=worst_case_value_wei,
        request_id=broker_request_id,
        request_digest=workload_request_digest,
        caller_public_key=caller_public_key,
        broker=broker_wholesale,
        daemon=daemon,
        clock=clock,
        settings=settings,
        sdk_identity=sdk_identity,
        spend_period_seconds=cfg.spend_period_seconds,
        spend_period_cap_wei=cfg.spend_period_cap_wei,
        gateway_session_id=gateway_session_id,
    )


# ---------------------------------------------------------------------------
# Refill orchestration (POST /v1/sessions/{id}/refill)
# ---------------------------------------------------------------------------


# Cap-status thresholds (per exec-plan 002 sub-decision 2): when any
# enabled cap crosses this fraction AND the projected next-mint would
# push it over, LOC sets ``will_refuse_next_refill=true`` in the
# refill response so the SDK can warn the customer one window early.
_CAP_IMMINENT_THRESHOLD = 0.95


async def _session_billed_so_far_wei(db: AsyncSession, session_id: uuid.UUID) -> Decimal:
    """Sum accepted issued value, excluding rotation-rejected envelopes."""
    result = await db.scalars(
        select(Payment.expected_value_wei).where(
            Payment.session_id == session_id,
            Payment.status != "refused",
        )
    )
    return Decimal(sum((Decimal(v) for v in result.all()), Decimal(0)))


async def _session_funded_units(db: AsyncSession, session_id: uuid.UUID) -> int:
    """Return the cumulative unit target funded across every session mint."""

    result = await db.scalars(
        select(Payment.work_units_requested).where(
            Payment.session_id == session_id,
            Payment.status != "refused",
        )
    )
    return sum(int(units) for units in result.all())


def _route_from_persisted_snapshot(session_row: PaymentSession) -> SelectedRoute:
    """Rehydrate the typed route without consulting mutable discovery state."""

    snapshot = RouteSnapshot.model_validate(session_row.route_snapshot)
    return SelectedRoute(
        worker_url=snapshot.broker_url,
        eth_address=snapshot.eth_address,
        capability=snapshot.capability,
        offering=snapshot.offering,
        price_per_work_unit_wei=snapshot.price_per_work_unit_wei,
        work_unit=snapshot.work_unit,
        units_per_price=snapshot.units_per_price,
        quote_id=snapshot.quote_id,
        quote_version=snapshot.quote_version,
        constraint_fingerprint=bytes.fromhex(snapshot.constraint_fingerprint),
        route_fingerprint=bytes.fromhex(snapshot.route_fingerprint),
        protocol=snapshot.protocol,
        settlement_keys=snapshot.settlement_keys,
        work_unit_estimator=snapshot.work_unit_estimator,
        extra=snapshot.extra,
    )


async def _refill_wholesale_session(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    user_id: uuid.UUID,
    requested_max_total_units: int,
    request_digest: bytes,
    request_id: str,
    broker: BrokerWholesaleAccountClient | None,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
    cfg: billing_service.ResolvedBillingConfig,
) -> RefillSessionResponse:
    """Increase one cumulative cap without minting a session-sized ticket."""

    if broker is None:
        raise DaemonUnavailable(
            daemon="wholesale-account", reason="broker account client is unavailable"
        )
    route = _route_from_persisted_snapshot(session_row)
    session_axes = route.session
    if session_axes is None or session_axes.refill != "extensible":
        raise RefillNotSupported

    authorization_id = f"loc-auth:{request_id}"
    revision_grant = await db.scalar(
        select(SpendAuthorizationGrant).where(
            SpendAuthorizationGrant.session_id == session_row.id,
            SpendAuthorizationGrant.authorization_id == authorization_id,
        )
    )
    if revision_grant is None:
        if requested_max_total_units <= session_row.max_total_units:
            raise InvalidSessionRequest(
                message="wholesale cap revision must increase max_total_units"
            )
        if session_row.authorization_id is None:
            raise InvalidSessionRequest(message="wholesale session has no current authorization")
        predecessor = await db.scalar(
            select(SpendAuthorizationGrant).where(
                SpendAuthorizationGrant.session_id == session_row.id,
                SpendAuthorizationGrant.authorization_id == session_row.authorization_id,
            )
        )
        if predecessor is None:
            raise InvalidSessionRequest(message="authorization predecessor is unavailable")
        new_wholesale_cap = _bill_value_wei(
            units=requested_max_total_units,
            amount_wei=route.price_per_work_unit_wei,
            per_units=route.units_per_price,
        )
        if session_row.customer_pricing is None or session_row.customer_max_debit_wei is None:
            raise InvalidSessionRequest(message="wholesale customer pricing is unavailable")
        pricing = CustomerPricingSnapshot.model_validate(session_row.customer_pricing)
        new_customer_cap = billing_service.calculate_customer_charge(
            pricing,
            actual_units=requested_max_total_units,
            wholesale_debit_wei=new_wholesale_cap,
        )
        incremental_hold = new_customer_cap - session_row.customer_max_debit_wei
        if incremental_hold <= 0:
            raise InvalidSessionRequest(message="cap revision did not increase customer exposure")
        next_revision = predecessor.revision + 1
        await billing_service.increase_customer_engagement_cap(
            db,
            user_id=user_id,
            engagement_id=session_row.id,
            revision=next_revision,
            amount_wei=incremental_hold,
            clock=clock,
            period_seconds=cfg.spend_period_seconds,
            cap_wei=cfg.spend_period_cap_wei,
        )
        now = clock.now()
        auth_request, auth_response = await payments_service.issue_route_locked_authorization(
            daemon=daemon,
            route=route,
            authorization_id=authorization_id,
            request_id=request_id,
            session_id=str(session_row.id),
            request_digest=request_digest,
            caller_public_key=bytes.fromhex(predecessor.caller_public_key),
            max_debit_wei=new_wholesale_cap,
            max_total_units=requested_max_total_units,
            not_before=now,
            expires_at=now + timedelta(minutes=5),
            chain_id=settings.wholesale_chain_id,
            revision=next_revision,
            predecessor_authorization_id=predecessor.authorization_id,
        )
        revision_grant = await record_spend_authorization_grant(
            db,
            engagement_id=session_row.id,
            user_id=user_id,
            route=route,
            request=auth_request,
            response=auth_response,
        )
        session_row.max_total_units = requested_max_total_units
        session_row.funded_value_wei = new_wholesale_cap
        session_row.customer_max_debit_wei = new_customer_cap
        session_row.refill_seq = next_revision
        await db.commit()
    elif (
        revision_grant.request_id != request_id
        or revision_grant.max_total_units != requested_max_total_units
        or revision_grant.request_digest != request_digest.hex()
    ):
        raise InvalidSessionRequest(message="authorization revision replay changed scope")

    plan = await _replenish_wholesale_account(
        db,
        route=route,
        payer_eth_address=revision_grant.payer_eth_address,
        mint_request_id=f"loc-account:{request_id}",
        correlation_id=str(session_row.id),
        broker=broker,
        daemon=daemon,
        clock=clock,
        settings=settings,
    )
    cap_status = await _compute_cap_status(
        db,
        session_row=session_row,
        user_id=user_id,
        next_mint_value_wei=Decimal(0),
        cfg=cfg,
        clock=clock,
    )
    return RefillSessionResponse(
        work_id=revision_grant.authorization_id,
        request_id=request_id,
        refill_seq=revision_grant.revision,
        spend_authorization=base64.b64encode(revision_grant.authorization_bytes).decode("ascii"),
        accounting_mode="wholesale_account",
        expected_value_wei=int(plan.shortfall_wei),
        funded_value_wei=int(plan.shortfall_wei),
        cap_status=cap_status,
    )


async def refill_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    observed_consumed_units: int | None,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
    request_id: str | None = None,
    max_total_units: int,
    workload_request_digest: bytes,
    broker_wholesale: BrokerWholesaleAccountClient | None = None,
) -> RefillSessionResponse:
    """Increase an open session's cumulative scoped authorization cap.

    The revision increases the customer hold as needed and may replenish a
    bounded shortfall in the shared payer-payee account. It never mints a
    session-sized ticket. ``observed_consumed_units`` remains advisory only.
    """
    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # 1. Session exists + ownership
    session_row = await db.scalar(
        select(PaymentSession).where(PaymentSession.id == session_id).with_for_update()
    )
    if session_row is None or session_row.user_id != user_id:
        raise SessionNotFound

    # 2. State check
    if session_row.state != SESSION_STATE_OPEN:
        raise SessionNotOpen(current_state=session_row.state)

    broker_request_id = request_id or str(uuid.uuid4())
    if session_row.accounting_mode != "wholesale_account":
        raise InvalidSessionRequest(
            message="legacy sessions cannot be refilled by the wholesale-only release"
        )
    return await _refill_wholesale_session(
        db,
        session_row=session_row,
        user_id=user_id,
        requested_max_total_units=max_total_units,
        request_digest=workload_request_digest,
        request_id=broker_request_id,
        broker=broker_wholesale,
        daemon=daemon,
        clock=clock,
        settings=settings,
        cfg=cfg,
    )


async def _compute_cap_status(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    user_id: uuid.UUID,
    next_mint_value_wei: Decimal,
    session_units_exhausted: bool = False,
    cfg: billing_service.ResolvedBillingConfig,
    clock: Clock,
) -> CapStatus:
    """Compute the per-cap headroom snapshot returned with refill 200.

    Percentages are over [0, 1]. Unconfigured caps surface as ``None``.
    ``will_refuse_next_refill`` is set when any *enabled* cap is at or
    above :data:`_CAP_IMMINENT_THRESHOLD` AND the projected next mint
    would push it over. Sets ``winddown_reason`` to the offending cap.
    """
    # Session pct: prefer the persisted billed_value_wei for closed
    # sessions (set by close_session/settle_job from actual_units);
    # for live sessions, fall back to summing payment EVs (the
    # running cumulative).
    if session_row.billed_value_wei is not None:
        session_billed = session_row.billed_value_wei
    else:
        session_billed = await _session_billed_so_far_wei(db, session_row.id)
    session_pct = float(session_billed / session_row.funded_value_wei)
    session_pct = min(max(session_pct, 0.0), 1.0)

    # Spend-period (enabled iff cap_wei > 0)
    spend_period_pct: float | None = None
    if cfg.spend_period_cap_wei > 0:
        room = await billing_service.remaining_window_room(
            db,
            user_id=user_id,
            clock=clock,
            period_seconds=cfg.spend_period_seconds,
            cap_wei=cfg.spend_period_cap_wei,
        )
        spent = Decimal(cfg.spend_period_cap_wei) - room
        spend_period_pct = float(spent / Decimal(cfg.spend_period_cap_wei))
        spend_period_pct = min(max(spend_period_pct, 0.0), 1.0)

    # User balance and operator-pool are deferred to a later PR
    # (need to track "starting balance" for a meaningful pct;
    # operator-pool cap is opt-in v1).
    user_balance_pct: float | None = None
    operator_pool_pct: float | None = None

    will_refuse, reason = _project_next_refusal(
        session_pct=session_pct,
        session_remaining_wei=session_row.funded_value_wei - session_billed,
        next_mint_value_wei=next_mint_value_wei,
        spend_period_pct=spend_period_pct,
        session_units_exhausted=session_units_exhausted,
    )

    return CapStatus(
        session_pct_used=session_pct,
        spend_period_pct_used=spend_period_pct,
        user_balance_pct_used=user_balance_pct,
        operator_pool_pct_used=operator_pool_pct,
        will_refuse_next_refill=will_refuse,
        winddown_reason=reason,
    )


def _project_next_refusal(
    *,
    session_pct: float,
    session_remaining_wei: Decimal,
    next_mint_value_wei: Decimal,
    spend_period_pct: float | None,
    session_units_exhausted: bool = False,
) -> tuple[bool, str | None]:
    """Predict whether the *next* refill request will be refused."""
    if session_units_exhausted:
        return True, "session_cap_imminent"
    if session_pct >= _CAP_IMMINENT_THRESHOLD and next_mint_value_wei > session_remaining_wei:
        return True, "session_cap_imminent"
    if spend_period_pct is not None and spend_period_pct >= _CAP_IMMINENT_THRESHOLD:
        return True, "spend_period_cap_imminent"
    return False, None


# ---------------------------------------------------------------------------
# Close orchestration (POST /v1/sessions/{id}/close)
# ---------------------------------------------------------------------------


def _infer_close_outcome(*, funded: Decimal, billed: Decimal) -> str:
    """Default outcome when SDK doesn't supply one.

    Mirrors the upstream `SettlementOutcome` enum:
      - EXACT          : billed == funded
      - OVERFUNDED     : billed < funded (the common path)
      - UNDERFUNDED    : billed > funded (broker exceeded the authorized
                        wholesale ceiling — invalid in normal operation)

    `STOPPED_AT_BUDGET` and `TOPPED_UP` are SDK-supplied; we don't
    infer them.
    """
    if billed > funded:
        return "UNDERFUNDED"
    if billed < funded:
        return "OVERFUNDED"
    return "EXACT"


async def _verify_close_settlement(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    settlement: dict[str, Any] | None,
    require_terminal: bool = True,
) -> VerifiedSessionSettlement:
    """Verify and bind an authoritative broker settlement."""

    if settlement is None:
        raise SessionSettlementVerificationFailed(reason="missing_settlement")
    if settlement.get("signature") is None:
        raise SessionSettlementVerificationFailed(reason="missing_signature")
    snapshot = session_row.route_snapshot or {}
    settlement_keys = snapshot.get("settlement_keys")
    if not isinstance(settlement_keys, list) or not settlement_keys:
        raise SessionSettlementVerificationFailed(reason="missing_delegation")
    predecessor_work_id = ""
    if session_row.rotation_generation > 0:
        if not session_row.predecessor_work_id:
            raise SessionSettlementVerificationFailed(reason="missing_rotation_predecessor")
        predecessor_work_id = session_row.predecessor_work_id
    payload = settlement.get("payload")
    authorization_hint = payload.get("authorization_id") if isinstance(payload, dict) else None
    if not isinstance(authorization_hint, str) or not authorization_hint:
        raise SessionSettlementVerificationFailed(reason="missing_authorization")
    grant = await db.scalar(
        select(SpendAuthorizationGrant).where(
            SpendAuthorizationGrant.session_id == session_row.id,
            SpendAuthorizationGrant.authorization_id == authorization_hint,
        )
    )
    if grant is None:
        raise SessionSettlementVerificationFailed(reason="missing_authorization")
    authorization_id = grant.authorization_id
    authorized_value_wei = int(grant.max_debit_wei)
    # A revision can be durably issued before aggregate funding succeeds. The
    # broker may therefore close against a still-delivered predecessor; bind
    # work_id to the cryptographically selected grant, not LOC's newest pointer.
    expected_work_id = grant.authorization_id
    amount_wei = int(snapshot["price_per_work_unit_wei"])
    try:
        return verify_session_settlement(
            settlement,
            settlement_keys=settlement_keys,
            expected=SessionSettlementExpectation(
                gateway_session_id=str(session_row.id),
                broker_session_id=session_row.broker_session_id,
                work_id=expected_work_id,
                predecessor_work_id=predecessor_work_id,
                rotation_generation=session_row.rotation_generation,
                work_unit=str(snapshot["work_unit"]),
                amount_wei=amount_wei,
                per_units=int(snapshot["units_per_price"]),
                quote_id=str(snapshot["quote_id"]),
                quote_version=int(snapshot["quote_version"]),
                constraint_fingerprint=bytes.fromhex(str(snapshot["constraint_fingerprint"])),
                route_fingerprint=bytes.fromhex(str(snapshot["route_fingerprint"])),
                funded_value_wei=int(session_row.funded_value_wei),
                last_settlement_seq=session_row.last_settlement_seq,
                authorization_id=authorization_id,
                authorized_value_wei=authorized_value_wei,
                require_terminal=require_terminal,
            ),
        )
    except (KeyError, TypeError, ValueError, SettlementVerificationError) as exc:
        reason = exc.code if isinstance(exc, SettlementVerificationError) else "invalid_snapshot"
        raise SessionSettlementVerificationFailed(reason=reason) from exc


async def close_session(  # noqa: PLR0915 — explicit settlement state machine
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    actual_units: int,
    outcome: str | None,
    settlement: dict[str, Any] | None,
    clock: Clock,
) -> CloseSessionResponse:
    """Explicitly close a session and finalize accounting.

    Pre-conditions:
      1. Session exists, belongs to caller's user.
      2. Session is in ``open`` or ``draining`` state. A second close
         on an already-closed session raises :class:`SessionNotOpen`.

    Performs (in order):
      1. transition_state to ``closed``.
      2. Verify the broker-signed terminal settlement against the pinned
         route keys, gateway session identity, rotation chain, and price.
      3. Compute ``refund_wei = funded_value_wei - billed_value_wei``
         (the worst-case encumbrance minus what was actually used).
      4. ``release_session_encumbrance(refund_wei)`` — credits the
         user balance back. No-op if refund_wei <= 0 (overrun case;
         operator absorbs).
      5. Update ``payment_session`` with billed_value_wei,
         actual_units, outcome.
      6. ``record_settlement(event_type='close')`` with the final
         numbers and any raw SettlementRecord from the SDK.
      7. Return the typed response.

    The signed broker record is authoritative. SDK-reported units and outcome
    are accepted only when they agree with that record.
    """
    # 1. Lookup + ownership
    session_row = await db.get(PaymentSession, session_id)
    if session_row is None or session_row.user_id != user_id:
        raise SessionNotFound

    if session_row.state == SESSION_STATE_CLOSED:
        raise SessionNotOpen(current_state=session_row.state)
    if session_row.accounting_mode != "wholesale_account":
        raise SessionSettlementVerificationFailed(reason="legacy_accounting_disabled")

    # 2. Compute billed + refund
    try:
        verified = await _verify_close_settlement(
            db,
            session_row=session_row,
            settlement=settlement,
        )
        if actual_units != verified.debited_units:
            raise SessionSettlementVerificationFailed(reason="work_units_mismatch")
    except SessionSettlementVerificationFailed as exc:
        await telemetry_events.emit_settlement_verification_failed(
            db,
            api_key_id=session_row.api_key_id,
            user_id=user_id,
            session_id=session_id,
            protocol=session_row.protocol,
            reason=str(exc.details.get("reason", "unknown")),
            clock=clock,
        )
        raise
    wholesale_billed_value_wei = Decimal(verified.billed_value_wei)
    if session_row.customer_pricing is None or session_row.customer_max_debit_wei is None:
        raise SessionSettlementVerificationFailed(reason="missing_customer_pricing")
    pricing = CustomerPricingSnapshot.model_validate(session_row.customer_pricing)
    billed_value_wei = billing_service.calculate_customer_charge(
        pricing,
        actual_units=verified.debited_units,
        wholesale_debit_wei=wholesale_billed_value_wei,
    )
    if billed_value_wei > session_row.customer_max_debit_wei:
        raise SessionSettlementVerificationFailed(reason="customer_cap_exceeded")
    refund_wei = session_row.customer_max_debit_wei - billed_value_wei

    # 3. Transition state (open or draining → closed)
    await transition_state(
        db,
        session_id,
        from_state=session_row.state,
        to_state=SESSION_STATE_CLOSED,
        clock=clock,
    )

    # 4. Release encumbrance (refund unused). Skip if billed exceeded
    # funded — operator absorbs that delta; no balance change.
    if refund_wei > 0:
        await billing_service.release_customer_engagement(
            db,
            user_id=user_id,
            engagement_id=session_row.id,
            amount_wei=refund_wei,
        )

    # 5. Finalize payment_session fields
    signed_outcome = verified.outcome
    if signed_outcome == "SETTLEMENT_OUTCOME_UNSPECIFIED":
        signed_outcome = _infer_close_outcome(
            funded=session_row.funded_value_wei, billed=billed_value_wei
        )
    if outcome is not None and outcome != signed_outcome:
        raise SessionSettlementVerificationFailed(reason="outcome_mismatch")
    final_outcome = signed_outcome
    session_row.actual_units = verified.debited_units
    session_row.billed_value_wei = billed_value_wei
    session_row.outcome = final_outcome
    session_row.broker_session_id = verified.broker_session_id
    session_row.last_settlement_seq = verified.settlement_seq
    diagnostics = {
        key: value
        for key, value in {
            "termination_reason": verified.termination_reason,
            "output_state": verified.output_state,
            "output_state_since": (
                verified.output_state_since.isoformat()
                if verified.output_state_since is not None
                else None
            ),
            "last_failure_code": verified.last_failure_code,
        }.items()
        if value is not None
    }
    prior_breakdown = dict(session_row.breakdown or {})
    prior_breakdown.pop("settlement_block", None)
    if diagnostics:
        prior_breakdown["broker_diagnostics"] = diagnostics
    session_row.breakdown = prior_breakdown or None
    payload = settlement.get("payload") if settlement is not None else None
    settled_authorization_id = (
        payload.get("authorization_id") if isinstance(payload, dict) else None
    )
    if not isinstance(settled_authorization_id, str) or not settled_authorization_id:
        raise SessionSettlementVerificationFailed(reason="missing_authorization")
    session_row.authorization_id = settled_authorization_id
    session_row.work_id = settled_authorization_id
    await mark_spend_authorization_settled(
        db,
        engagement_id=session_row.id,
        authorization_id=settled_authorization_id,
        clock=clock,
    )
    await db.flush()

    # 6. Append close settlement event
    await record_settlement(
        db,
        session_id,
        event_type="close",
        clock=clock,
        actual_units=verified.debited_units,
        billed_value_wei=billed_value_wei,
        outcome=final_outcome,
        raw_record=settlement,
    )

    # 7. Response
    assert session_row.closed_at is not None  # transition_state set it
    return CloseSessionResponse(
        session_id=session_row.id,
        work_id=session_row.work_id,
        actual_units=verified.debited_units,
        billed_value_wei=int(billed_value_wei),
        refund_wei=int(max(refund_wei, Decimal(0))),
        outcome=final_outcome,
        closed_at=session_row.closed_at,
    )


# ---------------------------------------------------------------------------
# Reconciliation janitor (background task)
# ---------------------------------------------------------------------------


DEFAULT_JANITOR_INTERVAL_SECONDS = 60


async def reconcile_spend_authorization_states(
    db: AsyncSession,
    *,
    broker: BrokerWholesaleAccountClient,
    clock: Clock,
    batch_limit: int = 100,
) -> int:
    """Advance local grants only from their locked broker's durable state.

    This does not settle customer billing: that still requires the signed
    workload settlement. It records the irrevocable receiver state needed to
    decide whether an old route reservation may eventually be retired safely.
    """

    grants = list(
        (
            await db.scalars(
                select(SpendAuthorizationGrant)
                .join(PaymentSession, PaymentSession.id == SpendAuthorizationGrant.session_id)
                .where(
                    PaymentSession.accounting_mode == "wholesale_account",
                    SpendAuthorizationGrant.state.in_(("issued", "admitted", "outcome_unknown")),
                )
                .order_by(SpendAuthorizationGrant.created_at.asc())
                .limit(batch_limit)
            )
        ).all()
    )
    changed = 0
    terminal = {
        SpendAuthorizationState.SETTLED,
        SpendAuthorizationState.EXPIRED_UNUSED,
        SpendAuthorizationState.SUPERSEDED,
    }
    for grant in grants:
        snapshot = grant.route_snapshot or {}
        broker_url = snapshot.get("broker_url")
        if not isinstance(broker_url, str) or not broker_url:
            continue
        try:
            observed = await broker.get_spend_authorization(
                broker_url=broker_url,
                payer_eth_address=grant.payer_eth_address,
                authorization_id=grant.authorization_id,
            )
        except BrokerWholesaleAccountError:
            continue
        next_state = observed.state.value
        if next_state == grant.state:
            continue
        # Receiver state is monotonic. Never let an inconsistent observation
        # resurrect a reservation or turn an admitted grant into unused expiry.
        if grant.state == "admitted" and observed.state in {
            SpendAuthorizationState.ISSUED,
            SpendAuthorizationState.EXPIRED_UNUSED,
        }:
            continue
        if grant.state == "outcome_unknown" and observed.state not in terminal:
            continue
        grant.state = next_state
        if observed.state in terminal:
            grant.retired_at = grant.retired_at or clock.now()
        changed += 1
    await db.flush()
    return changed


async def reconcile_open_sessions(
    db: AsyncSession,
    *,
    settlement_client: BrokerSettlementClient,
    clock: Clock,
    interval_seconds: int = DEFAULT_JANITOR_INTERVAL_SECONDS,
    batch_limit: int = 100,
) -> int:
    """Finalize silent sessions from broker-signed terminal settlements.

    The lookup uses LOC's globally unique ``payment_session.id`` as the
    Modules ``gateway_session_id``. ``work_id`` is intentionally never used:
    several broker sessions share the same payer-payee wholesale account.
    """
    cutoff = clock.now() - timedelta(seconds=interval_seconds)
    rows = list(
        (
            await db.scalars(
                select(PaymentSession)
                .where(
                    # Paid jobs share this table but settle through the
                    # request-ID exchange lookup in jobs.service; brokers
                    # key GET /v1/settlement by broker job id, so asking
                    # with a LOC job id only produces 401s.
                    PaymentSession.protocol == PAID_SESSION_PROTOCOL,
                    PaymentSession.accounting_mode == "wholesale_account",
                    PaymentSession.state.in_((SESSION_STATE_OPEN, SESSION_STATE_DRAINING)),
                    (PaymentSession.last_polled_at.is_(None))
                    | (PaymentSession.last_polled_at < cutoff),
                )
                .order_by(PaymentSession.last_polled_at.asc().nulls_first())
                .limit(batch_limit)
            )
        ).all()
    )

    finalized = 0
    for session_row in rows:
        snapshot = session_row.route_snapshot or {}
        broker_url = snapshot.get("broker_url", snapshot.get("worker_url"))
        if not isinstance(broker_url, str) or not broker_url:
            continue
        try:
            settlement = await settlement_client.get_settlement(
                broker_url=broker_url,
                gateway_session_id=session_row.id,
            )
        except BrokerSettlementQueryError:
            continue

        await mark_polled(db, session_row.id, clock=clock)
        if settlement is None:
            continue

        block = (session_row.breakdown or {}).get("settlement_block")
        signature = settlement.get("signature") if isinstance(settlement, dict) else None
        signature_value = signature.get("value") if isinstance(signature, dict) else None
        if isinstance(block, dict) and block.get("signature") == signature_value:
            # Same broker record that already failed against this snapshot;
            # nothing changed, so do not re-verify or re-report it.
            continue
        try:
            verified = await _verify_close_settlement(
                db,
                session_row=session_row,
                settlement=settlement,
                require_terminal=False,
            )
        except SessionSettlementVerificationFailed as exc:
            reason = str(exc.details.get("reason", "unknown"))
            if reason == "settlement_replay":
                # The broker's latest record is one LOC already applied
                # (the open or a refill). Nothing newer to verify yet; the
                # SDK close will bring the terminal record.
                continue
            session_row.breakdown = {
                **(session_row.breakdown or {}),
                "settlement_block": {
                    "reason": reason,
                    "signature": signature_value,
                    "first_seen": clock.now().isoformat(),
                    "last_seen": clock.now().isoformat(),
                    "attempts": 1,
                },
            }
            await db.flush()
            await telemetry_events.emit_settlement_verification_failed(
                db,
                api_key_id=session_row.api_key_id,
                user_id=session_row.user_id,
                session_id=session_row.id,
                protocol=session_row.protocol,
                reason=reason,
                clock=clock,
            )
            continue
        if verified.state != "closed":
            continue

        try:
            close_response = await close_session(
                db,
                session_id=session_row.id,
                user_id=session_row.user_id,
                actual_units=verified.debited_units,
                outcome=None,
                settlement=settlement,
                clock=clock,
            )
        except SessionNotOpen:
            continue
        finalized += 1
        opened_at = session_row.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=clock.now().tzinfo)
        await telemetry_events.emit_session_janitor_finalized(
            db,
            api_key_id=session_row.api_key_id,
            user_id=session_row.user_id,
            session_id=session_row.id,
            actual_units=close_response.actual_units,
            billed_value_wei=int(close_response.billed_value_wei),
            refund_wei=int(close_response.refund_wei),
            outcome=close_response.outcome,
            silence_duration_seconds=max(int((clock.now() - opened_at).total_seconds()), 0),
            clock=clock,
        )

    return finalized


# ---------------------------------------------------------------------------
# Read endpoint (GET /v1/sessions/{id})
# ---------------------------------------------------------------------------


async def get_session_status(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    clock: Clock,
    settings: Settings,
) -> SessionStatusResponse:
    """Return a snapshot of the session's current state + accounting.

    Raises :class:`SessionNotFound` if the session is missing or
    owned by a different user (uniform 404 — doesn't disclose
    existence).

    For ``open`` / ``draining`` sessions, ``cap_status`` is computed
    on the fly using the same logic refill responses use (without
    actually projecting a next mint — so ``will_refuse_next_refill``
    is best-effort from current pct only).

    For ``closed`` sessions, ``cap_status`` is ``None`` and the
    close fields (``actual_units``, ``outcome``, ``closed_at``) are
    populated.

    ``billed_value_wei`` is the sum of expected_value across all
    Payment rows for live sessions, or the final billed value for
    closed sessions.
    """
    session_row = await db.get(PaymentSession, session_id)
    if session_row is None or session_row.user_id != user_id:
        raise SessionNotFound

    is_live = session_row.state != SESSION_STATE_CLOSED

    cap_status: CapStatus | None = None
    if is_live:
        cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)
        # next_mint_value=0 here — we're not projecting an actual mint,
        # just reporting current headroom. Means
        # will_refuse_next_refill reflects only the threshold-crossing
        # state, not a projected-overrun.
        cap_status = await _compute_cap_status(
            db,
            session_row=session_row,
            user_id=user_id,
            next_mint_value_wei=Decimal(0),
            session_units_exhausted=(
                await _session_funded_units(db, session_id) >= session_row.max_total_units
            ),
            cfg=cfg,
            clock=clock,
        )

    # billed_value_wei: for closed sessions use the persisted final
    # value; for live use the cumulative payment EV.
    if session_row.billed_value_wei is not None:
        billed_wei = int(session_row.billed_value_wei)
    else:
        billed_wei = int(await _session_billed_so_far_wei(db, session_id))

    return SessionStatusResponse(
        session_id=session_row.id,
        work_id=session_row.work_id,
        capability=session_row.capability,
        offering=session_row.offering,
        protocol=session_row.protocol,
        state=session_row.state,
        estimated_units=session_row.estimated_units,
        max_total_units=session_row.max_total_units,
        funded_value_wei=int(session_row.funded_value_wei),
        billed_value_wei=billed_wei,
        refill_count=session_row.refill_seq,
        cap_status=cap_status,
        opened_at=session_row.opened_at,
        closed_at=session_row.closed_at,
        actual_units=session_row.actual_units,
        outcome=session_row.outcome,
    )


# ---------------------------------------------------------------------------
