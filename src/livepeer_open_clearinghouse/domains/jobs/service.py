"""Business logic for the jobs domain.

Composes the same primitives ``sessions.service`` uses
(``create_session``, ``transition_state``, ``record_settlement``,
``billing.encumber_for_session`` / ``release_session_encumbrance``)
but gated on the ``http-*@v0`` mode set instead of the case-(d)
session modes.

A job is just a short-lived ``payment_session`` row with a job-class
mode. The mint path mirrors ``sessions.service.open_session``; the
settle path mirrors ``sessions.service.close_session``. Differences:

  - Mode gate: JOB_MODES instead of SESSION_OPEN_MODES.
  - Initial mint is sized for ``max_total_units`` (jobs are
    one-shot — no refills — so the full worst case lives in the
    single ticket).
  - Response shape uses ``job_id`` naming (just sugar over
    ``session_id``) and exposes a single ``settle_endpoint``
    instead of refill+close pair.
"""

from __future__ import annotations

import base64
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.jobs.types import (
    CreateJobResponse,
    SettleJobResponse,
)
from livepeer_open_clearinghouse.domains.payments.repo import Payment
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
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

# Modes that POST /v1/jobs accepts. Long-running session modes go
# through POST /v1/sessions instead.
JOB_MODES: frozenset[str] = frozenset(
    {
        "http-reqresp@v0",
        "http-stream@v0",
        "http-multipart@v0",
    }
)


class ModeNotSupportedForJob(OpenClearinghouseError):
    """Mode is known upstream but isn't a job-class mode."""

    def __init__(self, *, mode: str) -> None:
        super().__init__(
            code="mode_not_supported_for_job",
            message=(
                f"mode {mode!r} is not a job mode; use POST /v1/sessions "
                "for ws-realtime / session-control-plus-media / live-session-* "
                "workloads"
            ),
            status_code=400,
        )


class JobNotFound(OpenClearinghouseError):
    def __init__(self) -> None:
        super().__init__(
            code="job_not_found",
            message="job not found",
            status_code=404,
        )


class JobAlreadySettled(OpenClearinghouseError):
    def __init__(self, *, current_state: str) -> None:
        super().__init__(
            code="job_already_settled",
            message=f"job is in state {current_state!r}; settle requires 'open'",
            status_code=409,
        )


def _settle_endpoint_for(job_id: uuid.UUID) -> str:
    return f"/v1/jobs/{job_id}/settle"


async def open_job(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    capability: str,
    offering: str,
    estimated_units: int,
    max_total_units: int | None,
    sdk_identity: str | None,
    registry: RegistryClient,
    daemon: PaymentDaemonClient,
    clock: Clock,
    settings: Settings,
) -> CreateJobResponse:
    """Open a one-shot job (cases a/b/c) under handoff mode.

    Behaves like ``sessions.service.open_session`` except the mode
    gate is :data:`JOB_MODES`, and the initial mint funds the full
    worst case (no refills are possible for jobs).

    Defaults ``max_total_units`` to ``estimated_units`` when the SDK
    doesn't supply it — typical for case (a) where the SDK knows
    exactly what it needs.
    """
    effective_max = max_total_units if max_total_units is not None else estimated_units
    if effective_max < estimated_units:
        raise sessions_service.InvalidSessionRequest(
            message="max_total_units must be >= estimated_units"
        )

    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # Discovery
    route = await registry.select(capability, offering)
    if route is None:
        raise NoRouteAvailable(capability=capability, offering=offering)

    # Mode declaration + validation
    mode = route.interaction_mode
    if mode is None:
        raise sessions_service.ModeNotDeclared(capability=capability, offering=offering)
    if mode not in JOB_MODES:
        raise ModeNotSupportedForJob(mode=mode)

    # Worst case = full max_total_units (jobs have no refills, so the
    # initial mint funds the entire envelope).
    price_wei = Decimal(route.price_per_work_unit_wei)
    units_per_price = Decimal(route.units_per_price or 1)
    worst_case_value_wei = price_wei * Decimal(effective_max) / units_per_price

    # Up-front balance check
    balance = await billing_service.get_balance(db, user_id=user_id)
    if balance.amount_wei < worst_case_value_wei:
        raise InsufficientCredit(
            available_wei=int(balance.amount_wei),
            required_wei=int(worst_case_value_wei),
        )

    # Daemon mint sized for the full worst case (one ticket covers
    # the whole job).
    daemon_request = CreatePaymentRequest(
        recipient=sessions_service._eth_address_to_bytes(route.eth_address),
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
            funded_value_wei=worst_case_value_wei,
            estimated_units=estimated_units,
            max_total_units=effective_max,
        ),
    )
    try:
        daemon_response = await daemon.create_payment(daemon_request)
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc

    # Write payment_session (one row backs each job).
    job_row = await sessions_service.create_session(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        work_id=daemon_response.work_id,
        capability=route.capability,
        offering=route.offering,
        mode=mode,
        estimated_units=estimated_units,
        max_total_units=effective_max,
        funded_value_wei=worst_case_value_wei,
        clock=clock,
        sdk_identity=sdk_identity,
    )

    # Write Payment row tied to the job.
    payment = Payment(
        user_id=user_id,
        api_key_id=api_key_id,
        session_id=job_row.id,
        work_id=daemon_response.work_id,
        recipient_eth_address=route.eth_address,
        capability=route.capability,
        offering=route.offering,
        work_units_requested=estimated_units,
        price_per_work_unit_wei=price_wei,
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    db.add(payment)
    await db.flush()

    # Encumber worst case
    await billing_service.encumber_for_session(
        db,
        user_id=user_id,
        payment_id=payment.id,
        amount_wei=worst_case_value_wei,
        clock=clock,
        period_seconds=cfg.spend_period_seconds,
        cap_wei=cfg.spend_period_cap_wei,
    )

    return CreateJobResponse(
        job_id=job_row.id,
        work_id=daemon_response.work_id,
        broker_url=route.worker_url,
        mode=mode,
        payment_envelope=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=int(daemon_response.expected_value),
        funded_value_wei=int(worst_case_value_wei),
        settle_endpoint=_settle_endpoint_for(job_row.id),
        opened_at=job_row.opened_at,
    )


async def settle_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    actual_units: int,
    outcome: str | None,
    settlement: dict[str, Any] | None,
    clock: Clock,
) -> SettleJobResponse:
    """Settle a job after the SDK has called the broker and read the
    Livepeer-Work-Units header (or trailer for http-stream).

    Mirrors ``sessions.service.close_session`` — same accounting,
    same encumbrance release, same settlement-event write. The mode
    gate is enforced at open time, so settle works uniformly for any
    job-class session.
    """
    # Lookup + ownership
    job_row = await db.get(PaymentSession, job_id)
    if job_row is None or job_row.user_id != user_id:
        raise JobNotFound

    if job_row.state == sessions_service.SESSION_STATE_CLOSED:
        raise JobAlreadySettled(current_state=job_row.state)

    # Initial payment for price context
    initial_payment_row = await db.scalar(
        select(Payment)
        .where(Payment.session_id == job_id)
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    if initial_payment_row is None:
        raise JobNotFound  # defensive

    price_wei = Decimal(initial_payment_row.price_per_work_unit_wei)
    billed_value_wei = price_wei * Decimal(actual_units)
    refund_wei = job_row.funded_value_wei - billed_value_wei

    # Transition state
    await sessions_service.transition_state(
        db,
        job_id,
        from_state=job_row.state,
        to_state=sessions_service.SESSION_STATE_CLOSED,
        clock=clock,
    )

    # Release encumbrance if there's unused value to refund.
    if refund_wei > 0:
        await billing_service.release_session_encumbrance(
            db,
            user_id=user_id,
            payment_id=initial_payment_row.id,
            amount_wei=refund_wei,
        )

    # Finalize fields
    final_outcome = outcome or sessions_service._infer_close_outcome(
        funded=job_row.funded_value_wei, billed=billed_value_wei
    )
    job_row.actual_units = actual_units
    job_row.billed_value_wei = billed_value_wei
    job_row.outcome = final_outcome
    await db.flush()

    # Settlement event
    await sessions_service.record_settlement(
        db,
        job_id,
        event_type="close",
        clock=clock,
        actual_units=actual_units,
        billed_value_wei=billed_value_wei,
        outcome=final_outcome,
        raw_record=settlement,
    )

    assert job_row.closed_at is not None
    return SettleJobResponse(
        job_id=job_row.id,
        work_id=job_row.work_id,
        actual_units=actual_units,
        billed_value_wei=int(billed_value_wei),
        refund_wei=int(max(refund_wei, Decimal(0))),
        outcome=final_outcome,
        closed_at=job_row.closed_at,
    )
