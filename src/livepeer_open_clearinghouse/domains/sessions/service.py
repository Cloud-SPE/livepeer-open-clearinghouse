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
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.domains.sessions.types import (
    CapStatus,
    CloseSessionResponse,
    CreateSessionResponse,
    RefillSessionResponse,
    SessionAxesView,
    SessionStatusResponse,
)
from livepeer_open_clearinghouse.domains.telemetry import server_events as telemetry_events
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseError,
    SpendCapExceeded,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerSettlementClient,
    BrokerSettlementQueryError,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    CreatePaymentResponse,
    FundingIntent,
    MintOutcomeUnknown,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.registry_daemon import RegistryClient
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
) -> PaymentSession:
    """Create a new session in the ``open`` state and return it.

    Caller is responsible for any encumbrance accounting on the
    user's balance — this function only writes the session row.
    """
    now = clock.now()
    row = PaymentSession(
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
        opened_at=now,
        sdk_identity=sdk_identity,
    )
    session.add(row)
    await session.flush()
    return row


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
    descriptor_schema: str | None = None,
    request_id: str | None = None,
) -> CreateSessionResponse:
    """Open a long-running session (case d) under handoff mode.

    Composes:

      1. Sanity-check the request shape (``max_total_units`` vs.
         ``estimated_runway_units``).
      2. Route discovery via the registry.
      3. Require the authoritative ``paid-session/v1`` protocol.
      4. Compute ``worst_case_value_wei = max_total_units x price``.
      5. Mint the initial ticket via the payer-daemon sized to
         ``initial_runway_value_wei = estimated_runway_units x price``
         (NOT worst-case — the daemon decides per-ticket sizing;
         worst-case is for the LOC-side encumbrance only).
      6. Write the ``payment_session`` row (``state=open``,
         ``funded_value_wei=worst_case``).
      7. Write the ``Payment`` row for the initial ticket, linked
         via ``session_id``.
      8. Encumber ``worst_case_value_wei`` from the user balance via
         ``billing.encumber_for_session`` (also counts against the
         spend-period cap).
      9. Return the typed response.

    Returns 4xx via typed exceptions on validation failures; 5xx via
    :class:`DaemonUnavailable` if the daemon call fails.
    """
    broker_request_id = request_id or str(uuid.uuid4())
    if max_total_units < estimated_runway_units:
        raise InvalidSessionRequest(message="max_total_units must be >= estimated_runway_units")

    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # ---- 2. Discovery
    route = await registry.select(capability, offering)
    if route is None:
        raise NoRouteAvailable(capability=capability, offering=offering)

    # ---- 3. Protocol declaration + validation
    protocol = route.protocol
    if protocol != PAID_SESSION_PROTOCOL:
        raise ProtocolNotSupportedForSession(protocol=protocol)
    session_axes = route.session
    if session_axes is None:  # pragma: no cover - SelectedRoute validates this
        raise InvalidSessionRequest(message="session route declaration is unavailable")
    if descriptor_schema is not None and session_axes.descriptor_schema != descriptor_schema:
        raise InvalidSessionRequest(
            message=(
                f"offering declares descriptor schema {session_axes.descriptor_schema!r}; "
                f"the client requested {descriptor_schema!r}"
            )
        )

    # ---- 4. Worst-case encumbrance + initial mint sizing
    price_wei = Decimal(route.price_per_work_unit_wei)
    worst_case_value_wei = _bill_value_wei(
        units=max_total_units,
        amount_wei=price_wei,
        per_units=route.units_per_price,
    )
    initial_runway_value_wei = _bill_value_wei(
        units=estimated_runway_units,
        amount_wei=price_wei,
        per_units=route.units_per_price,
    )

    # Up-front balance check against the worst case so we fail fast
    # before paying the daemon. (The encumber call later will also
    # check, but that path raises InsufficientCredit AFTER mint —
    # we'd rather not leave a paid-but-not-encumbered ticket
    # outstanding.)
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

    # ---- 5. Daemon call (initial ticket sized for runway)
    mint_started_ns = time.monotonic_ns()
    mint_request_id = f"loc:{broker_request_id}"
    daemon_request = CreatePaymentRequest(
        mint_request_id=mint_request_id,
        recipient=_eth_address_to_bytes(route.eth_address),
        ticket_params_base_url=route.worker_url,
        accepted_price=AcceptedPrice(
            capability=route.capability,
            offering=route.offering,
            price_per_unit_wei=route.price_per_work_unit_wei,
            units_per_price=route.units_per_price,
            work_unit_name=route.work_unit,
            quote_ref=QuoteRef(
                quote_id=route.quote_id,
                quote_version=route.quote_version,
                constraint_fingerprint=route.constraint_fingerprint,
                route_fingerprint=route.route_fingerprint,
            ),
        ),
        funding=FundingIntent(
            funded_value_wei=initial_runway_value_wei,
            estimated_units=estimated_runway_units,
            max_total_units=max_total_units,
        ),
    )
    try:
        daemon_response = await daemon.create_payment(daemon_request)
    except MintOutcomeUnknown as exc:
        from livepeer_open_clearinghouse.errors import IdempotencyOutcomeUnknown  # noqa: PLC0415

        raise IdempotencyOutcomeUnknown from exc
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc

    # ---- 6. Write payment_session
    session_row = await create_session(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        work_id=daemon_response.work_id,
        capability=route.capability,
        offering=route.offering,
        protocol=protocol,
        route_snapshot=route.snapshot(),
        broker_request_id=broker_request_id,
        estimated_units=estimated_runway_units,
        max_total_units=max_total_units,
        funded_value_wei=worst_case_value_wei,
        clock=clock,
        sdk_identity=sdk_identity,
    )

    # ---- 7. Write Payment for the initial ticket, linked to session
    payment = Payment(
        user_id=user_id,
        api_key_id=api_key_id,
        session_id=session_row.id,
        work_id=daemon_response.work_id,
        mint_request_id=mint_request_id,
        recipient_eth_address=route.eth_address,
        capability=route.capability,
        offering=route.offering,
        work_units_requested=estimated_runway_units,
        price_per_work_unit_wei=price_wei,
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        creation_round=daemon_response.creation_round,
        expires_after_round=daemon_response.expires_after_round,
        ticket_validity_period=daemon_response.ticket_validity_period,
        ticket_validity_period_observed_at=(daemon_response.ticket_validity_period_observed_at),
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    db.add(payment)
    await db.flush()

    # ---- 8. Encumber worst-case from the user balance
    try:
        await billing_service.encumber_for_session(
            db,
            user_id=user_id,
            payment_id=payment.id,
            amount_wei=worst_case_value_wei,
            clock=clock,
            period_seconds=cfg.spend_period_seconds,
            cap_wei=cfg.spend_period_cap_wei,
        )
    except SpendCapExceeded as exc:
        cap_wei = int(exc.details.get("cap_wei", 0))
        spent_wei = int(exc.details.get("would_be_spent_wei", 0))
        await telemetry_events.emit_mint_refused(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            capability=capability,
            offering=offering,
            which_cap="spend_period",
            remaining_wei=max(cap_wei - spent_wei, 0),
            clock=clock,
        )
        raise

    # ---- 9. Emit server.mint_served + sdk_sha_mismatch if applicable
    mint_latency_ms = (time.monotonic_ns() - mint_started_ns) // 1_000_000
    await telemetry_events.emit_mint_served(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        capability=capability,
        offering=offering,
        protocol=protocol,
        estimated_units=estimated_runway_units,
        funded_value_wei=int(worst_case_value_wei),
        mint_latency_ms=int(mint_latency_ms),
        correlation_id=session_row.id,
        clock=clock,
    )
    await telemetry_events.emit_sha_mismatch_if_unapproved(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        sdk_identity=sdk_identity,
        clock=clock,
    )

    # ---- 10. Return the typed response
    return CreateSessionResponse(
        session_id=session_row.id,
        request_id=broker_request_id,
        work_id=daemon_response.work_id,
        broker_url=route.worker_url,
        protocol=protocol,
        session=SessionAxesView.model_validate(session_axes.model_dump(mode="json")),
        payment_envelope=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=int(daemon_response.expected_value),
        funded_value_wei=int(worst_case_value_wei),
        refill_endpoint=_refill_endpoint_for(session_row.id),
        close_endpoint=_close_endpoint_for(session_row.id),
        opened_at=session_row.opened_at,
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


async def _next_refill_funding(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    price_wei: Decimal,
    per_units: int,
) -> tuple[int, Decimal]:
    """Size the next refill as a delta on the cumulative billing curve."""

    funded_units = await _session_funded_units(db, session_row.id)
    remaining_units = session_row.max_total_units - funded_units
    if remaining_units <= 0:
        return 0, Decimal(0)
    next_units = min(session_row.estimated_units, remaining_units)
    before = _bill_value_wei(units=funded_units, amount_wei=price_wei, per_units=per_units)
    after = _bill_value_wei(
        units=funded_units + next_units,
        amount_wei=price_wei,
        per_units=per_units,
    )
    return next_units, after - before


def _refill_snapshot(session_row: PaymentSession) -> dict[str, Any]:
    """Return a usable v1 route snapshot or refuse the refill."""

    snapshot = session_row.route_snapshot or {}
    axes = snapshot.get("axes")
    if not isinstance(axes, dict):
        raise InvalidSessionRequest(message="session route declaration is unavailable")
    if axes.get("refill", "extensible") == "bounded":
        raise RefillNotSupported
    return snapshot


async def _prepare_rotation(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    initial_payment_row: Payment,
    rebind_from: str | None,
    replaces_request_id: str | None,
    broker_request_id: str,
    daemon: PaymentDaemonClient,
) -> Payment | None:
    """Bind payee rejection feedback to one issued predecessor payment."""

    if (rebind_from is None) != (replaces_request_id is None):
        raise InvalidSessionRequest(
            message="rebind_from and replaces_request_id must be supplied together"
        )
    if rebind_from is None:
        return None
    if rebind_from != session_row.work_id:
        raise InvalidSessionRequest(message="rotation predecessor does not match session")
    if broker_request_id == replaces_request_id:
        raise InvalidSessionRequest(message="rotation requires a fresh request identity")

    replaced_payment = await db.scalar(
        select(Payment).where(
            Payment.session_id == session_row.id,
            Payment.mint_request_id == f"loc:{replaces_request_id}",
            Payment.work_id == rebind_from,
            Payment.status == "issued",
        )
    )
    if replaced_payment is None:
        raise InvalidSessionRequest(message="rejected rotation payment is unavailable")

    replaced_payment.status = "refused"
    replaced_payment.refused_reason = "invalid_recipient_rand"
    replaced_payment.refunded_wei = replaced_payment.expected_value_wei
    try:
        await daemon.report_invalid_recipient_rand(
            work_id=rebind_from,
            capability=initial_payment_row.capability,
            offering=initial_payment_row.offering,
        )
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc
    return replaced_payment


async def _mint_refill(
    *,
    initial_payment_row: Payment,
    snapshot: dict[str, Any],
    next_mint_units: int,
    next_mint_value_wei: Decimal,
    per_units: int,
    broker_request_id: str,
    daemon: PaymentDaemonClient,
) -> CreatePaymentResponse:
    """Mint one ordinary top-up or fresh rotation successor."""

    daemon_request = CreatePaymentRequest(
        mint_request_id=f"loc:{broker_request_id}",
        recipient=_eth_address_to_bytes(initial_payment_row.recipient_eth_address),
        # The payer requires the payee's ticket-params route on every
        # CreatePayment call, including a refill that should reuse its
        # existing payment identity. An empty URL does not mean "reuse";
        # it is an invalid request. Keep using the route pinned at open so
        # a refill neither rediscovers nor drifts to another payee.
        ticket_params_base_url=str(snapshot.get("worker_url", "")),
        accepted_price=AcceptedPrice(
            capability=initial_payment_row.capability,
            offering=initial_payment_row.offering,
            price_per_unit_wei=Decimal(initial_payment_row.price_per_work_unit_wei),
            units_per_price=per_units,
            work_unit_name=str(snapshot.get("work_unit", "")),
            quote_ref=QuoteRef(
                quote_id=str(snapshot.get("quote_id", "")),
                quote_version=int(snapshot.get("quote_version", 0)),
                constraint_fingerprint=bytes.fromhex(
                    str(snapshot.get("constraint_fingerprint", ""))
                ),
                route_fingerprint=bytes.fromhex(str(snapshot.get("route_fingerprint", ""))),
            ),
        ),
        funding=FundingIntent(
            funded_value_wei=next_mint_value_wei,
            estimated_units=next_mint_units,
            max_total_units=next_mint_units,
        ),
    )
    try:
        return await daemon.create_payment(daemon_request)
    except MintOutcomeUnknown as exc:
        from livepeer_open_clearinghouse.errors import IdempotencyOutcomeUnknown  # noqa: PLC0415

        raise IdempotencyOutcomeUnknown from exc
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc


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
    rebind_from: str | None = None,
    replaces_request_id: str | None = None,
) -> RefillSessionResponse:
    """Mint a top-up bound to an existing session's work_id.

    Pre-conditions (in order):

      1. Session exists, belongs to caller's user.
      2. Session is in ``open`` state.
      3. The persisted ``session.refill`` declaration is ``extensible``.
         Bounded sessions reject with 400.
      4. Cumulative minted EV + next mint EV <= session funded
         (worst-case). If not, refuse with ``cap_reached: session``.
      5. Spend-period cap has room for the next mint. If not,
         refuse with ``cap_reached: spend_period`` (the encumbrance
         at open recorded the worst case against the window, so
         this is usually a no-op — but cap could shrink between
         opens, so we re-check).

    On success: mints via daemon (re-using the same
    ``(recipient, capability, offering, funded_value_wei,
    broker_url)`` session-cache key per the daemon's convention so
    the new ticket attaches to the same ``work_id``), writes a new
    Payment row tied to the session via ``session_id``, increments
    ``refill_seq``, and returns the envelope plus a fresh
    ``cap_status``.

    Notes:
      - Worst-case encumbrance was done at open; no additional
        balance debit at refill (the funded value is already
        reserved).
      - ``observed_consumed_units`` is advisory only. It is logged for
        triage but not used to size the mint.
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

    # 3. Declared-axis check — only extensible sessions refill.
    snapshot = _refill_snapshot(session_row)

    # Pull pricing context from the initial Payment; price and route are
    # pinned for the logical session across recipient rotation.
    # (the initial mint's price; all refills use the same price).
    initial_payment_row = await db.scalar(
        select(Payment)
        .where(Payment.session_id == session_id)
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    if initial_payment_row is None:
        # Should never happen — open_session writes one. Defensive.
        raise SessionNotFound

    broker_request_id = request_id or str(uuid.uuid4())
    replaced_payment = await _prepare_rotation(
        db,
        session_row=session_row,
        initial_payment_row=initial_payment_row,
        rebind_from=rebind_from,
        replaces_request_id=replaces_request_id,
        broker_request_id=broker_request_id,
        daemon=daemon,
    )

    price_wei = Decimal(initial_payment_row.price_per_work_unit_wei)
    per_units = int(snapshot.get("units_per_price", 0))
    if per_units < 1:
        raise InvalidSessionRequest(message="session price denominator is unavailable")
    if replaced_payment is None:
        next_mint_units, next_mint_value_wei = await _next_refill_funding(
            db,
            session_row=session_row,
            price_wei=price_wei,
            per_units=per_units,
        )
    else:
        next_mint_units = int(replaced_payment.work_units_requested)
        next_mint_value_wei = Decimal(replaced_payment.funded_value_wei)
    if next_mint_units == 0:
        await telemetry_events.emit_refill_denied(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            session_id=session_id,
            refill_seq=session_row.refill_seq + 1,
            which_cap="session",
            remaining_wei=0,
            clock=clock,
        )
        raise SessionCapReached(
            which="session",
            remaining_wei=0,
            advice=(
                "session reached max_total_units; "
                "open a new session with a higher max_total_units to continue"
            ),
        )

    # 4. Per-session cap check
    billed_so_far = await _session_billed_so_far_wei(db, session_id)
    session_remaining = session_row.funded_value_wei - billed_so_far
    if next_mint_value_wei > session_remaining:
        await telemetry_events.emit_refill_denied(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            session_id=session_id,
            refill_seq=session_row.refill_seq + 1,
            which_cap="session",
            remaining_wei=int(session_remaining),
            clock=clock,
        )
        raise SessionCapReached(
            which="session",
            remaining_wei=int(session_remaining),
            advice=(
                "session would exceed max_total_units; "
                "open a new session with a higher max_total_units to continue"
            ),
        )

    # 5. Spend-period cap check
    period_room = await billing_service.remaining_window_room(
        db,
        user_id=user_id,
        clock=clock,
        period_seconds=cfg.spend_period_seconds,
        cap_wei=cfg.spend_period_cap_wei,
    )
    if next_mint_value_wei > period_room:
        period_remaining_int = int(period_room) if period_room != Decimal("Infinity") else 0
        await telemetry_events.emit_refill_denied(
            db,
            api_key_id=api_key_id,
            user_id=user_id,
            session_id=session_id,
            refill_seq=session_row.refill_seq + 1,
            which_cap="spend_period",
            remaining_wei=period_remaining_int,
            clock=clock,
        )
        raise SessionCapReached(
            which="spend_period",
            remaining_wei=period_remaining_int,
            advice=(
                "rolling spend-period cap reached; raise the cap at "
                "/portal/billing or wait for period rollover"
            ),
        )

    # ---- Daemon call. Same session-cache key as the initial mint so
    # the daemon reuses recipient_rand_hash and increments nonce.
    mint_request_id = f"loc:{broker_request_id}"
    daemon_response = await _mint_refill(
        initial_payment_row=initial_payment_row,
        snapshot=snapshot,
        next_mint_units=next_mint_units,
        next_mint_value_wei=next_mint_value_wei,
        per_units=per_units,
        broker_request_id=broker_request_id,
        daemon=daemon,
    )

    if replaced_payment is not None and daemon_response.work_id == rebind_from:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason="rotation mint reused the rejected work_id"
        )

    # Ordinary refills remain attached to the logical session work ID.
    # Only the explicit rotation path adopts the daemon's fresh recipient.
    current_work_id = (
        daemon_response.work_id if replaced_payment is not None else session_row.work_id
    )

    # ---- Persist the top-up Payment row + bump the LOC refill ordinal
    refill_payment = Payment(
        user_id=user_id,
        api_key_id=api_key_id,
        session_id=session_id,
        work_id=current_work_id,
        mint_request_id=mint_request_id,
        recipient_eth_address=initial_payment_row.recipient_eth_address,
        capability=initial_payment_row.capability,
        offering=initial_payment_row.offering,
        work_units_requested=next_mint_units,
        price_per_work_unit_wei=price_wei,
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        creation_round=daemon_response.creation_round,
        expires_after_round=daemon_response.expires_after_round,
        ticket_validity_period=daemon_response.ticket_validity_period,
        ticket_validity_period_observed_at=(daemon_response.ticket_validity_period_observed_at),
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    db.add(refill_payment)
    session_row.refill_seq = session_row.refill_seq + 1
    if replaced_payment is not None:
        session_row.work_id = current_work_id
        session_row.rotation_generation += 1
    await db.flush()

    # ---- Record a payment_settlement event
    await record_settlement(
        db,
        session_id,
        event_type="refill_granted",
        clock=clock,
        billed_value_wei=daemon_response.expected_value,
        raw_record={
            "refill_seq": session_row.refill_seq,
            "observed_consumed_units": observed_consumed_units,
            "rebind_from": rebind_from,
            "rotation_generation": session_row.rotation_generation,
        },
    )

    # ---- Build cap_status block
    cap_status = await _compute_cap_status(
        db,
        session_row=session_row,
        user_id=user_id,
        next_mint_value_wei=next_mint_value_wei,
        session_units_exhausted=(
            await _session_funded_units(db, session_id) >= session_row.max_total_units
        ),
        cfg=cfg,
        clock=clock,
    )

    await telemetry_events.emit_refill_served(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        session_id=session_id,
        refill_seq=session_row.refill_seq,
        funded_value_wei=int(daemon_response.funded_value_wei),
        cap_status=cap_status.model_dump(),
        clock=clock,
    )

    return RefillSessionResponse(
        work_id=current_work_id,
        request_id=broker_request_id,
        refill_seq=session_row.refill_seq,
        payment_envelope=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=int(daemon_response.expected_value),
        funded_value_wei=int(daemon_response.funded_value_wei),
        cap_status=cap_status,
        rebind_from=rebind_from,
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
      - UNDERFUNDED    : billed > funded (broker debited more than the
                        ticket face value covered — unusual but possible)

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
    initial_payment_row: Payment,
    settlement: dict[str, Any] | None,
    require_terminal: bool = True,
) -> VerifiedSessionSettlement:
    """Verify and bind an authoritative broker settlement."""

    if settlement is None:
        raise SessionSettlementVerificationFailed(reason="missing_settlement")
    snapshot = session_row.route_snapshot or {}
    settlement_keys = snapshot.get("settlement_keys")
    if not isinstance(settlement_keys, list) or not settlement_keys:
        raise SessionSettlementVerificationFailed(reason="missing_delegation")
    predecessor_work_id = ""
    if session_row.rotation_generation > 0:
        predecessor = await db.scalar(
            select(Payment)
            .where(
                Payment.session_id == session_row.id,
                Payment.status == "refused",
                Payment.refused_reason == "invalid_recipient_rand",
            )
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        if predecessor is None:
            raise SessionSettlementVerificationFailed(reason="missing_rotation_predecessor")
        predecessor_work_id = predecessor.work_id
    try:
        return verify_session_settlement(
            settlement,
            settlement_keys=settlement_keys,
            expected=SessionSettlementExpectation(
                gateway_session_id=str(session_row.id),
                broker_session_id=session_row.broker_session_id,
                work_id=session_row.work_id,
                predecessor_work_id=predecessor_work_id,
                rotation_generation=session_row.rotation_generation,
                work_unit=str(snapshot["work_unit"]),
                amount_wei=int(initial_payment_row.price_per_work_unit_wei),
                per_units=int(snapshot["units_per_price"]),
                funded_value_wei=int(session_row.funded_value_wei),
                last_settlement_seq=session_row.last_settlement_seq,
                require_terminal=require_terminal,
            ),
        )
    except (KeyError, TypeError, ValueError, SettlementVerificationError) as exc:
        reason = exc.code if isinstance(exc, SettlementVerificationError) else "invalid_snapshot"
        raise SessionSettlementVerificationFailed(reason=reason) from exc


async def close_session(
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

    # 2. Compute billed + refund
    initial_payment_row = await db.scalar(
        select(Payment)
        .where(Payment.session_id == session_id)
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    if initial_payment_row is None:
        raise SessionNotFound  # defensive — open writes one

    verified = await _verify_close_settlement(
        db,
        session_row=session_row,
        initial_payment_row=initial_payment_row,
        settlement=settlement,
    )
    if actual_units != verified.debited_units:
        raise SessionSettlementVerificationFailed(reason="work_units_mismatch")
    billed_value_wei = Decimal(verified.billed_value_wei)
    refund_wei = session_row.funded_value_wei - billed_value_wei

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
        await billing_service.release_session_encumbrance(
            db,
            user_id=user_id,
            payment_id=initial_payment_row.id,
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
    several broker sessions may share the same payer ticket identity.
    """
    cutoff = clock.now() - timedelta(seconds=interval_seconds)
    rows = list(
        (
            await db.scalars(
                select(PaymentSession)
                .where(
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
        broker_url = snapshot.get("worker_url")
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

        initial_payment = await db.scalar(
            select(Payment)
            .where(Payment.session_id == session_row.id)
            .order_by(Payment.created_at.asc())
            .limit(1)
        )
        if initial_payment is None:
            continue
        try:
            verified = await _verify_close_settlement(
                db,
                session_row=session_row,
                initial_payment_row=initial_payment,
                settlement=settlement,
                require_terminal=False,
            )
        except SessionSettlementVerificationFailed:
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
            billed_value_wei=close_response.billed_value_wei,
            refund_wei=close_response.refund_wei,
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
