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
    SessionStatusResponse,
)
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


class RefillNotSupportedForMode(OpenClearinghouseError):
    """The session's mode is in the (d-bounded) set — no topup possible."""

    def __init__(self, *, mode: str) -> None:
        super().__init__(
            code="refill_not_supported_for_mode",
            message=(
                f"mode {mode!r} does not support mid-session topup; "
                "this session is bounded by its initial mint and will "
                "end when the funded runway is exhausted"
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


# ---------------------------------------------------------------------------
# Refill orchestration (POST /v1/sessions/{id}/refill)
# ---------------------------------------------------------------------------


# Cap-status thresholds (per exec-plan 002 sub-decision 2): when any
# enabled cap crosses this fraction AND the projected next-mint would
# push it over, LOC sets ``will_refuse_next_refill=true`` in the
# refill response so the SDK can warn the customer one window early.
_CAP_IMMINENT_THRESHOLD = 0.95


async def _session_billed_so_far_wei(db: AsyncSession, session_id: uuid.UUID) -> Decimal:
    """Sum ``expected_value_wei`` across all Payment rows tied to this session."""
    result = await db.scalars(
        select(Payment.expected_value_wei).where(Payment.session_id == session_id)
    )
    return Decimal(sum((Decimal(v) for v in result.all()), Decimal(0)))


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
) -> RefillSessionResponse:
    """Mint a top-up bound to an existing session's work_id.

    Pre-conditions (in order):

      1. Session exists, belongs to caller's user.
      2. Session is in ``open`` state.
      3. Session's mode is in :data:`SESSION_OPEN_MODES` AND NOT
         in the (d-bounded) set (currently just ``ws-realtime@v0``).
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
    ``last_debit_seq``, and returns the envelope plus a fresh
    ``cap_status``.

    Notes:
      - Worst-case encumbrance was done at open; no additional
        balance debit at refill (the funded value is already
        reserved).
      - ``observed_consumed_units`` is advisory only — the daemon's
        ledger is authoritative; the SDK's hint is logged for
        triage but not used to size the mint.
    """
    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # 1. Session exists + ownership
    session_row = await db.get(PaymentSession, session_id)
    if session_row is None or session_row.user_id != user_id:
        raise SessionNotFound

    # 2. State check
    if session_row.state != SESSION_STATE_OPEN:
        raise SessionNotOpen(current_state=session_row.state)

    # 3. Mode check — refill only works on (d-extensible)
    if session_row.mode == "ws-realtime@v0":
        raise RefillNotSupportedForMode(mode=session_row.mode)

    # Pull pricing context from the most recent Payment on this session
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

    price_wei = Decimal(initial_payment_row.price_per_work_unit_wei)
    next_mint_units = session_row.estimated_units  # refill chunk = runway size
    next_mint_value_wei = price_wei * Decimal(next_mint_units)

    # 4. Per-session cap check
    billed_so_far = await _session_billed_so_far_wei(db, session_id)
    session_remaining = session_row.funded_value_wei - billed_so_far
    if next_mint_value_wei > session_remaining:
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
        raise SessionCapReached(
            which="spend_period",
            remaining_wei=int(period_room) if period_room != Decimal("Infinity") else 0,
            advice=(
                "rolling spend-period cap reached; raise the cap at "
                "/portal/billing or wait for period rollover"
            ),
        )

    # ---- Daemon call. Same session-cache key as the initial mint so
    # the daemon reuses recipient_rand_hash and increments nonce.
    daemon_request = CreatePaymentRequest(
        recipient=_eth_address_to_bytes(initial_payment_row.recipient_eth_address),
        ticket_params_base_url="",  # daemon uses its cached value
        accepted_price=AcceptedPrice(
            capability=initial_payment_row.capability,
            offering=initial_payment_row.offering,
            price_per_unit_wei=price_wei,
            units_per_price=1,
            work_unit_name="",
            quote_ref=QuoteRef(
                quote_id="",
                quote_version=0,
                constraint_fingerprint=b"\x00" * 32,
                route_fingerprint=b"\x00" * 32,
            ),
        ),
        funding=FundingIntent(
            funded_value_wei=next_mint_value_wei,
            estimated_units=next_mint_units,
            max_total_units=next_mint_units,
        ),
    )
    try:
        daemon_response = await daemon.create_payment(daemon_request)
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc

    # ---- Persist the top-up Payment row + bump last_debit_seq
    refill_payment = Payment(
        user_id=user_id,
        api_key_id=api_key_id,
        session_id=session_id,
        work_id=session_row.work_id,
        recipient_eth_address=initial_payment_row.recipient_eth_address,
        capability=initial_payment_row.capability,
        offering=initial_payment_row.offering,
        work_units_requested=next_mint_units,
        price_per_work_unit_wei=price_wei,
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    db.add(refill_payment)
    session_row.last_debit_seq = session_row.last_debit_seq + 1
    await db.flush()

    # ---- Record a payment_settlement event
    await record_settlement(
        db,
        session_id,
        event_type="refill_granted",
        clock=clock,
        billed_value_wei=daemon_response.expected_value,
        raw_record={
            "refill_seq": session_row.last_debit_seq,
            "observed_consumed_units": observed_consumed_units,
        },
    )

    # ---- Build cap_status block
    cap_status = await _compute_cap_status(
        db,
        session_row=session_row,
        user_id=user_id,
        next_mint_value_wei=next_mint_value_wei,
        cfg=cfg,
        clock=clock,
    )

    return RefillSessionResponse(
        work_id=session_row.work_id,
        refill_seq=session_row.last_debit_seq,
        payment_envelope=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=int(daemon_response.expected_value),
        funded_value_wei=int(daemon_response.funded_value_wei),
        cap_status=cap_status,
    )


async def _compute_cap_status(
    db: AsyncSession,
    *,
    session_row: PaymentSession,
    user_id: uuid.UUID,
    next_mint_value_wei: Decimal,
    cfg: billing_service.ResolvedBillingConfig,
    clock: Clock,
) -> CapStatus:
    """Compute the per-cap headroom snapshot returned with refill 200.

    Percentages are over [0, 1]. Unconfigured caps surface as ``None``.
    ``will_refuse_next_refill`` is set when any *enabled* cap is at or
    above :data:`_CAP_IMMINENT_THRESHOLD` AND the projected next mint
    would push it over. Sets ``winddown_reason`` to the offending cap.
    """
    # Session (always enabled): include the just-minted refill in the sum.
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
) -> tuple[bool, str | None]:
    """Predict whether the *next* refill request will be refused."""
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
      2. Compute ``billed_value_wei = actual_units x price`` (price
         read from the initial mint's Payment row).
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

    Per the trust model: ``actual_units`` is SDK-reported and trusted
    on this synchronous path. The reconciliation janitor (PR-8) does
    the daemon cross-check via ``GetSessionDebits`` and corrects any
    divergence. v1 daemon client does not yet expose GetSessionDebits;
    once it does, this function will also verify inline.
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

    price_wei = Decimal(initial_payment_row.price_per_work_unit_wei)
    billed_value_wei = price_wei * Decimal(actual_units)
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
    final_outcome = outcome or _infer_close_outcome(
        funded=session_row.funded_value_wei, billed=billed_value_wei
    )
    session_row.actual_units = actual_units
    session_row.billed_value_wei = billed_value_wei
    session_row.outcome = final_outcome
    await db.flush()

    # 6. Append close settlement event
    await record_settlement(
        db,
        session_id,
        event_type="close",
        clock=clock,
        actual_units=actual_units,
        billed_value_wei=billed_value_wei,
        outcome=final_outcome,
        raw_record=settlement,
    )

    # 7. Response
    assert session_row.closed_at is not None  # transition_state set it
    return CloseSessionResponse(
        session_id=session_row.id,
        work_id=session_row.work_id,
        actual_units=actual_units,
        billed_value_wei=int(billed_value_wei),
        refund_wei=int(max(refund_wei, Decimal(0))),
        outcome=final_outcome,
        closed_at=session_row.closed_at,
    )


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
        mode=session_row.mode,
        state=session_row.state,
        estimated_units=session_row.estimated_units,
        max_total_units=session_row.max_total_units,
        funded_value_wei=int(session_row.funded_value_wei),
        billed_value_wei=billed_wei,
        refill_count=session_row.last_debit_seq,
        cap_status=cap_status,
        opened_at=session_row.opened_at,
        closed_at=session_row.closed_at,
        actual_units=session_row.actual_units,
        outcome=session_row.outcome,
    )


# ---------------------------------------------------------------------------
# Reconciliation janitor (background task)
# ---------------------------------------------------------------------------


# Default cadence for the per-session daemon poll (overridable via the
# scheduler-job registration call site in main.py). The doc proposed 60s
# as the safety-net interval; we expose it here as the public default.
DEFAULT_JANITOR_INTERVAL_SECONDS = 60


async def reconcile_open_sessions(
    db: AsyncSession,
    *,
    daemon: PaymentDaemonClient,
    clock: Clock,
    interval_seconds: int = DEFAULT_JANITOR_INTERVAL_SECONDS,
    batch_limit: int = 100,
) -> int:
    """Walk open sessions and reconcile against the daemon's ledger.

    For each open ``payment_session`` whose ``last_polled_at`` is
    older than ``interval_seconds`` (or NULL):

      1. Look up the most-recent Payment on the session to get the
         sender address (already encoded into work_id at mint).
      2. Call ``daemon.get_session_debits(sender, work_id)``.
      3. ``mark_polled`` to update ``last_polled_at`` regardless of
         outcome (so we don't tight-loop on flaky polls).
      4. If ``closed=True`` and our row is still open: finalize via
         :func:`close_session` with the daemon's
         ``total_work_units`` as authoritative. This is the
         silent-SDK / crashed-customer recovery path.
      5. If still open and the SDK has reported nothing recently,
         just log; no action — the session continues until either
         the SDK closes it OR the broker does and we observe
         ``closed=True``.

    Returns the number of sessions reconciled to ``closed`` state
    this pass. Batches at ``batch_limit`` so a backlog doesn't
    block the scheduler tick.
    """
    # Build the candidates query: open sessions whose last_polled_at
    # is older than (now - interval) OR NULL. We use the composite
    # index ix_payment_session_state_last_polled_at.
    now = clock.now()
    cutoff = now - timedelta(seconds=interval_seconds)

    rows_result = await db.scalars(
        select(PaymentSession)
        .where(
            PaymentSession.state == SESSION_STATE_OPEN,
            (PaymentSession.last_polled_at.is_(None)) | (PaymentSession.last_polled_at < cutoff),
        )
        .order_by(PaymentSession.last_polled_at.asc().nulls_first())
        .limit(batch_limit)
    )
    rows = list(rows_result.all())

    finalized = 0
    for ps in rows:
        # Find the initial Payment to get the recipient + sender address.
        # We use sender = bytes from the payer-daemon side; today our
        # daemon client doesn't expose the sender (it's the daemon's own
        # signing key), so we pass empty bytes and rely on the daemon
        # to match by work_id alone. When the real daemon needs the
        # sender for lookup, we'll thread it through here.
        try:
            debits = await daemon.get_session_debits(sender=b"", work_id=ps.work_id)
        except Exception:  # noqa: S112 — transient daemon failure; retry next tick
            # The scheduler's outer wrapper logs the failure for the
            # whole pass; per-session detail goes into telemetry once
            # PR-N wires it up.
            continue

        # Update last_polled_at unconditionally.
        await mark_polled(db, ps.id, clock=clock)

        # The daemon ledger reported the session is closed. We have to
        # finalize on our side. Use the daemon's authoritative
        # total_work_units, no outcome (close_session will infer one).
        if debits.closed:
            try:
                await close_session(
                    db,
                    session_id=ps.id,
                    user_id=ps.user_id,
                    actual_units=int(debits.total_work_units),
                    outcome=None,
                    settlement={"reconciled_by": "janitor"},
                    clock=clock,
                )
                finalized += 1
            except SessionNotOpen:
                # Raced with an explicit SDK close between the poll and
                # the finalize. That's fine.
                continue

    return finalized
