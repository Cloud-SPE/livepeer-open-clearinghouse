"""Business logic for the sessions domain.

PR-3 of exec-plan 002 ships the building blocks:

  - ``create_session`` writes a new ``payment_session`` row in
    ``open`` state.
  - ``get_session`` / ``get_session_by_work_id`` retrieve.
  - ``transition_state`` enforces the lifecycle state machine.
  - ``record_settlement`` appends a ``payment_settlement`` event.
  - ``mark_polled`` updates ``last_polled_at`` for the janitor.

The actual ``POST /v1/sessions`` handler, refill mint flow, and
janitor task land in subsequent PRs. These helpers are written so
those callers can compose them without re-implementing the state
machine or repo queries.
"""

from __future__ import annotations

import base64
import uuid
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
from livepeer_open_clearinghouse.domains.sessions.types import CreateSessionResponse
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseError,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    FundingIntent,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.registry_daemon import RegistryClient
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


class ModeNotDeclared(OpenClearinghouseError):
    """Offering didn't declare an interaction_mode in its registry extra."""

    def __init__(self, *, capability: str, offering: str) -> None:
        super().__init__(
            code="mode_not_declared",
            message=(
                f"offering {capability}/{offering} does not declare an "
                "interaction_mode; cannot open a session"
            ),
            status_code=400,
        )


class ModeNotSupportedForSession(OpenClearinghouseError):
    """Mode is known upstream but isn't a session-open mode (case d)."""

    def __init__(self, *, mode: str) -> None:
        super().__init__(
            code="mode_not_supported_for_session",
            message=(
                f"mode {mode!r} is not a long-running session mode; use "
                "POST /v1/jobs for atomic / streaming / multipart workloads"
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


# Modes that POST /v1/sessions accepts. The http-* modes are
# single-shot and go through POST /v1/jobs (Phase 2 PR-N).
SESSION_OPEN_MODES: frozenset[str] = frozenset(
    {
        "ws-realtime@v0",
        "session-control-plus-media@v0",
        "rtmp-ingress-hls-egress@v0",
        "live-session-remote-runner@v0",
        "live-session-gateway-ingest@v0",
    }
)


_ETH_ADDRESS_HEX_LEN = 40  # 20 bytes hex-encoded


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
    mode: str,
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
        mode=mode,
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
) -> CreateSessionResponse:
    """Open a long-running session (case d) under handoff mode.

    Composes:

      1. Sanity-check the request shape (``max_total_units`` vs.
         ``estimated_runway_units``).
      2. Route discovery via the registry; read the mode from
         ``route.extra["interaction_mode"]``.
      3. Validate the mode is one of :data:`SESSION_OPEN_MODES`.
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
    if max_total_units < estimated_runway_units:
        raise InvalidSessionRequest(message="max_total_units must be >= estimated_runway_units")

    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # ---- 2. Discovery
    route = await registry.select(capability, offering)
    if route is None:
        raise NoRouteAvailable(capability=capability, offering=offering)

    # ---- 3. Mode declaration + validation
    mode = route.interaction_mode
    if mode is None:
        raise ModeNotDeclared(capability=capability, offering=offering)
    if mode not in SESSION_OPEN_MODES:
        raise ModeNotSupportedForSession(mode=mode)

    # ---- 4. Worst-case encumbrance + initial mint sizing
    price_wei = Decimal(route.price_per_work_unit_wei)
    units_per_price = Decimal(route.units_per_price or 1)
    worst_case_value_wei = price_wei * Decimal(max_total_units) / units_per_price
    initial_runway_value_wei = price_wei * Decimal(estimated_runway_units) / units_per_price

    # Up-front balance check against the worst case so we fail fast
    # before paying the daemon. (The encumber call later will also
    # check, but that path raises InsufficientCredit AFTER mint —
    # we'd rather not leave a paid-but-not-encumbered ticket
    # outstanding.)
    balance = await billing_service.get_balance(db, user_id=user_id)
    if balance.amount_wei < worst_case_value_wei:
        raise InsufficientCredit(
            available_wei=int(balance.amount_wei),
            required_wei=int(worst_case_value_wei),
        )

    # ---- 5. Daemon call (initial ticket sized for runway)
    daemon_request = CreatePaymentRequest(
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
        mode=mode,
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
        recipient_eth_address=route.eth_address,
        capability=route.capability,
        offering=route.offering,
        work_units_requested=estimated_runway_units,
        price_per_work_unit_wei=price_wei,
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    db.add(payment)
    await db.flush()

    # ---- 8. Encumber worst-case from the user balance
    await billing_service.encumber_for_session(
        db,
        user_id=user_id,
        payment_id=payment.id,
        amount_wei=worst_case_value_wei,
        clock=clock,
        period_seconds=cfg.spend_period_seconds,
        cap_wei=cfg.spend_period_cap_wei,
    )

    # ---- 9. Return the typed response
    return CreateSessionResponse(
        session_id=session_row.id,
        work_id=daemon_response.work_id,
        broker_url=route.worker_url,
        mode=mode,
        payment_envelope=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=int(daemon_response.expected_value),
        funded_value_wei=int(worst_case_value_wei),
        refill_endpoint=_refill_endpoint_for(session_row.id),
        close_endpoint=_close_endpoint_for(session_row.id),
        opened_at=session_row.opened_at,
    )
