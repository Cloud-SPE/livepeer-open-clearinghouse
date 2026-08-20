"""Business logic for the jobs domain.

Composes the same primitives ``sessions.service`` uses
(``create_session``, ``transition_state``, ``record_settlement``,
``billing.encumber_for_session`` / ``release_session_encumbrance``)
but gated on the authoritative ``paid-job/v1`` protocol.

A job is just a short-lived ``payment_session`` row with a job-class
protocol. The mint path mirrors ``sessions.service.open_session``; the
settle path mirrors ``sessions.service.close_session``. Differences:

  - Protocol gate: ``paid-job/v1`` instead of ``paid-session/v1``.
  - Initial mint is sized for ``max_total_units`` (jobs are
    one-shot — no refills — so the full worst case lives in the
    single ticket).
  - Response shape uses ``job_id`` naming (just sugar over
    ``session_id``) and exposes a single ``settle_endpoint``
    instead of refill+close pair.
"""

from __future__ import annotations

import base64
import time
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
from livepeer_open_clearinghouse.domains.telemetry import server_events as telemetry_events
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseError,
    SpendCapExceeded,
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

PAID_JOB_PROTOCOL = "paid-job/v1"


class ProtocolNotSupportedForJob(OpenClearinghouseError):
    """The selected route is not a paid-job/v1 offering."""

    def __init__(self, *, protocol: str) -> None:
        super().__init__(
            code="protocol_not_supported_for_job",
            message=(
                f"protocol {protocol!r} is not accepted by POST /v1/jobs; "
                "paid sessions use POST /v1/sessions"
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
    request_id: str | None = None,
) -> CreateJobResponse:
    """Open a one-shot job (cases a/b/c) under handoff mode.

        Behaves like ``sessions.service.open_session`` except the protocol
    gate is ``paid-job/v1``, and the initial mint funds the full
    worst case (no refills are possible for jobs).

    Defaults ``max_total_units`` to ``estimated_units`` when the SDK
    doesn't supply it — typical for case (a) where the SDK knows
    exactly what it needs.
    """
    broker_request_id = request_id or str(uuid.uuid4())
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

    protocol = route.protocol
    if protocol != PAID_JOB_PROTOCOL:
        raise ProtocolNotSupportedForJob(protocol=protocol)

    # Worst case = full max_total_units (jobs have no refills, so the
    # initial mint funds the entire envelope).
    price_wei = Decimal(route.price_per_work_unit_wei)
    worst_case_value_wei = sessions_service._bill_value_wei(
        units=effective_max,
        amount_wei=price_wei,
        per_units=route.units_per_price,
    )

    # Up-front balance check
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

    # Daemon mint sized for the full worst case (one ticket covers
    # the whole job).
    mint_started_ns = time.monotonic_ns()
    mint_request_id = f"loc:{broker_request_id}"
    daemon_request = CreatePaymentRequest(
        mint_request_id=mint_request_id,
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
        protocol=protocol,
        route_snapshot=route.snapshot(),
        broker_request_id=broker_request_id,
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
        mint_request_id=mint_request_id,
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

    mint_latency_ms = (time.monotonic_ns() - mint_started_ns) // 1_000_000
    await telemetry_events.emit_mint_served(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        capability=capability,
        offering=offering,
        protocol=protocol,
        estimated_units=estimated_units,
        funded_value_wei=int(worst_case_value_wei),
        mint_latency_ms=int(mint_latency_ms),
        correlation_id=job_row.id,
        clock=clock,
    )
    await telemetry_events.emit_sha_mismatch_if_unapproved(
        db,
        api_key_id=api_key_id,
        user_id=user_id,
        sdk_identity=sdk_identity,
        clock=clock,
    )

    return CreateJobResponse(
        job_id=job_row.id,
        request_id=broker_request_id,
        work_id=daemon_response.work_id,
        broker_url=route.worker_url,
        protocol=protocol,
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
    settings: Settings,
) -> SettleJobResponse:
    """Settle a job after the SDK has called the broker and read the
    Livepeer-Work-Units header (or trailer for http-stream).

    Mirrors ``sessions.service.close_session`` — same accounting,
    same encumbrance release, same settlement-event write. The protocol
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
    snapshot = job_row.route_snapshot or {}
    billed_value_wei = sessions_service._bill_value_wei(
        units=actual_units,
        amount_wei=price_wei,
        per_units=int(snapshot.get("units_per_price", 1)),
    )
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

    # Cap snapshot for the SDK to surface "you're at N% of your
    # monthly cap" UX after the job. Project next_mint_value=0 — the
    # session is closed, so will_refuse_next_refill should reflect
    # absolute cap-fraction (e.g., spend-period cap at 95%+), not
    # per-session headroom.
    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)
    cap_status = await sessions_service._compute_cap_status(
        db,
        session_row=job_row,
        user_id=user_id,
        next_mint_value_wei=Decimal(0),
        cfg=cfg,
        clock=clock,
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
        cap_status=cap_status,
    )
