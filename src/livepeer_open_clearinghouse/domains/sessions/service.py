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

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)
from livepeer_open_clearinghouse.providers.clock import Clock

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
