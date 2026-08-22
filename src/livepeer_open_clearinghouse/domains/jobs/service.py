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
from datetime import timedelta
from decimal import Decimal
from typing import Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.jobs.types import (
    CreateJobResponse,
    JobStatusResponse,
    SettleJobResponse,
    SettlementEnvelope,
)
from livepeer_open_clearinghouse.domains.payments.repo import (
    Payment,
    PaymentDaemonDepositSnapshot,
)
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
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerExchangeOutcome,
    BrokerExchangeResult,
    BrokerSettlementClient,
    BrokerSettlementQueryError,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    FundingIntent,
    MintOutcomeUnknown,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.registry_daemon import RegistryClient
from livepeer_open_clearinghouse.providers.settlement_verification import (
    JobSettlementExpectation,
    SettlementVerificationError,
    verify_job_settlement,
)
from livepeer_open_clearinghouse.providers.telemetry import (
    job_reconciliation_observations_total,
    job_terminal_accounting_total,
)
from livepeer_open_clearinghouse.settings import Settings

PAID_JOB_PROTOCOL = "paid-job/v1"
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 60


class _RecoveredSettlementClaims(TypedDict):
    job_id: str
    work_unit: str
    actual_units: int
    outcome: str


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


class TransportNotSupportedForJob(OpenClearinghouseError):
    """The selected offering does not declare the requested transport."""

    def __init__(self, *, transport: str, declared: frozenset[str]) -> None:
        super().__init__(
            code="protocol_transport_unsupported",
            message=f"transport {transport!r} is not declared by the selected offering",
            status_code=400,
            details={"transport": transport, "declared_transports": sorted(declared)},
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


class WorkUnitMismatch(OpenClearinghouseError):
    """The broker's terminal unit echo differs from the pinned route unit."""

    def __init__(self, *, expected: str, received: str) -> None:
        super().__init__(
            code="work_unit_mismatch",
            message=f"broker reported work unit {received!r}; expected {expected!r}",
            status_code=409,
            details={"expected": expected, "received": received},
        )


class SettlementVerificationFailed(OpenClearinghouseError):
    """A broker claim cannot authorize a financial state change."""

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            code="settlement_verification_failed",
            message="broker settlement verification failed",
            status_code=409,
            details={"reason": reason},
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
    transport: Literal["unary", "stream", "multipart"] = "unary",
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
    job_axes = route.job
    if job_axes is None:  # pragma: no cover - enforced by SelectedRoute validation
        raise RuntimeError("paid-job/v1 route has no job axes")
    if transport not in job_axes.transports:
        raise TransportNotSupportedForJob(
            transport=transport,
            declared=frozenset(job_axes.transports),
        )

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
    except MintOutcomeUnknown as exc:
        from livepeer_open_clearinghouse.errors import IdempotencyOutcomeUnknown  # noqa: PLC0415

        raise IdempotencyOutcomeUnknown from exc
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc
    if (
        daemon_response.creation_round <= 0
        or daemon_response.ticket_validity_period <= 0
        or daemon_response.expires_after_round
        != daemon_response.creation_round + daemon_response.ticket_validity_period - 1
    ):
        raise DaemonUnavailable(
            daemon="payment-daemon",
            reason="invalid payment envelope expiry",
        )

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
        transport=transport,
        work_unit=route.work_unit,
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
    broker_job_id: str,
    work_unit: str,
    outcome: str | None,
    settlement: SettlementEnvelope,
    clock: Clock,
    settings: Settings,
) -> SettleJobResponse:
    """Settle a job from the broker's signed terminal claim.

    Mirrors ``sessions.service.close_session`` — same accounting,
    same encumbrance release, same settlement-event write. The protocol
    gate is enforced at open time, so settle works uniformly for any
    job-class session.
    """
    # Lookup + ownership
    job_row = await db.scalar(
        select(PaymentSession)
        .where(PaymentSession.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
    expected_work_unit = str(snapshot.get("work_unit", ""))
    if work_unit != expected_work_unit:
        raise WorkUnitMismatch(expected=expected_work_unit, received=work_unit)
    try:
        request_id = job_row.broker_request_id
        if not request_id:
            raise SettlementVerificationError(
                "missing_request_id", "job has no durable broker request id"
            )
        settlement_keys = snapshot["settlement_keys"]
        if not isinstance(settlement_keys, list) or not settlement_keys:
            raise SettlementVerificationError(
                "missing_delegation", "route snapshot has no settlement keys"
            )
        verified = verify_job_settlement(
            settlement.model_dump(mode="python"),
            settlement_keys=settlement_keys,
            expected=JobSettlementExpectation(
                request_id=request_id,
                job_id=broker_job_id,
                work_id=job_row.work_id,
                work_unit=expected_work_unit,
                actual_units=actual_units,
                max_total_units=job_row.max_total_units,
                funded_value_wei=int(job_row.funded_value_wei),
                amount_wei=int(price_wei),
                per_units=int(snapshot["units_per_price"]),
                quote_id=str(snapshot["quote_id"]),
                quote_version=int(snapshot["quote_version"]),
                constraint_fingerprint=bytes.fromhex(str(snapshot["constraint_fingerprint"])),
                route_fingerprint=bytes.fromhex(str(snapshot["route_fingerprint"])),
            ),
        )
    except (KeyError, TypeError, ValueError, SettlementVerificationError) as exc:
        reason = exc.code if isinstance(exc, SettlementVerificationError) else "invalid_snapshot"
        raise SettlementVerificationFailed(reason=reason) from exc
    if outcome is not None and outcome != verified.outcome:
        raise SettlementVerificationFailed(reason="outcome_mismatch")
    billed_value_wei = Decimal(verified.billed_value_wei)
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
    final_outcome = verified.outcome
    job_row.actual_units = actual_units
    job_row.billed_value_wei = billed_value_wei
    job_row.outcome = final_outcome
    job_row.breakdown = {
        **(job_row.breakdown or {}),
        "broker_job_id": broker_job_id,
        "work_unit": work_unit,
    }
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
        raw_record={
            "broker_job_id": broker_job_id,
            "work_unit": work_unit,
            "settlement": settlement.model_dump(mode="json"),
        },
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


async def reconcile_open_jobs(
    db: AsyncSession,
    *,
    settlement_client: BrokerSettlementClient,
    clock: Clock,
    settings: Settings,
    interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    batch_limit: int = 100,
) -> int:
    """Recover paid-job outcomes using only LOC's durable request ID.

    Broker outcome fields are hints. Only the embedded settlement, after
    normal signature/delegation/identity verification by ``settle_job``, may
    close a job or release encumbrance.
    """

    cutoff = clock.now() - timedelta(seconds=interval_seconds)
    rows = list(
        (
            await db.scalars(
                select(PaymentSession)
                .where(
                    PaymentSession.protocol == PAID_JOB_PROTOCOL,
                    PaymentSession.state == sessions_service.SESSION_STATE_OPEN,
                    (PaymentSession.last_polled_at.is_(None))
                    | (PaymentSession.last_polled_at < cutoff),
                )
                .order_by(PaymentSession.last_polled_at.asc().nulls_first())
                .limit(batch_limit)
            )
        ).all()
    )

    finalized = 0
    for job_row in rows:
        request_id = job_row.broker_request_id
        snapshot = job_row.route_snapshot or {}
        broker_url = snapshot.get("worker_url")
        if not request_id or not isinstance(broker_url, str) or not broker_url:
            continue
        try:
            exchange = await settlement_client.get_job_exchange(
                broker_url=broker_url,
                request_id=request_id,
            )
        except BrokerSettlementQueryError:
            continue

        job_reconciliation_observations_total.labels(outcome=exchange.outcome.value).inc()
        job_row.last_polled_at = clock.now()
        job_row.breakdown = {
            **(job_row.breakdown or {}),
            "broker_exchange": _exchange_audit_record(exchange),
        }
        await db.flush()
        settled = False
        if exchange.outcome is BrokerExchangeOutcome.SETTLED:
            claims = _recovered_settlement_claims(exchange)
            if claims is not None and exchange.settlement is not None:
                try:
                    await settle_job(
                        db,
                        job_id=job_row.id,
                        user_id=job_row.user_id,
                        actual_units=claims["actual_units"],
                        broker_job_id=claims["job_id"],
                        work_unit=claims["work_unit"],
                        outcome=claims["outcome"],
                        settlement=SettlementEnvelope.model_validate(exchange.settlement),
                        clock=clock,
                        settings=settings,
                    )
                    settled = True
                except (
                    JobAlreadySettled,
                    SettlementVerificationFailed,
                    WorkUnitMismatch,
                    ValueError,
                ):
                    # Keep the encumbrance intact. The audit snapshot makes a bad
                    # broker claim observable without granting it financial authority.
                    pass
        if settled:
            job_terminal_accounting_total.labels(terminal_kind="broker_settled").inc()
            finalized += 1
            continue

        # These outcomes are still moving and must outlive every operational
        # deadline. Charging while delivery/accounting is active would turn a
        # recoverable exact settlement into an avoidable full charge.
        if exchange.outcome in (
            BrokerExchangeOutcome.ACCOUNTING_PENDING,
            BrokerExchangeOutcome.IN_FLIGHT,
        ):
            continue
        if settings.job_conservative_charge_after_seconds <= 0:
            continue
        charged = await finalize_conservative_full_charge(
            db,
            job_id=job_row.id,
            clock=clock,
            deadline_seconds=settings.job_conservative_charge_after_seconds,
            evidence=_exchange_audit_record(exchange),
        )
        if charged:
            job_terminal_accounting_total.labels(terminal_kind="conservative_full_charge").inc()
            finalized += 1

    return finalized


def _recovered_settlement_claims(
    exchange: BrokerExchangeResult,
) -> _RecoveredSettlementClaims | None:
    settlement = exchange.settlement
    if settlement is None:
        return None
    payload = settlement.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        job_id = payload["job_id"]
        work_unit = payload["work_unit_name"]
        actual_units = int(payload["actual_units"])
        outcome = payload["outcome"]
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(work_unit, str)
        or not work_unit
        or actual_units < 0
        or not isinstance(outcome, str)
        or not outcome
    ):
        return None
    return {
        "job_id": job_id,
        "work_unit": work_unit,
        "actual_units": actual_units,
        "outcome": outcome,
    }


def _exchange_audit_record(exchange: BrokerExchangeResult) -> dict[str, object]:
    """Persist distinctions without treating unsigned hints as accounting."""

    record: dict[str, object] = {
        "request_id": exchange.request_id,
        "outcome": exchange.outcome.value,
    }
    for field in (
        "job_id",
        "state",
        "status",
        "work_units",
        "unit",
        "debit_attempts",
        "deadline",
        "ended_at",
        "detail",
    ):
        value = getattr(exchange, field)
        if value is not None:
            record[field] = value
    if exchange.non_admission is not None:
        record["non_admission"] = exchange.non_admission
    if exchange.settlement is not None:
        record["settlement"] = exchange.settlement
    return record


async def finalize_conservative_full_charge(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    clock: Clock,
    deadline_seconds: int,
    evidence: dict[str, object],
) -> bool:
    """Atomically close one unresolved job without claiming broker usage.

    Returns ``True`` only for the transaction that wins the open-to-closed
    transition. A concurrent verified settlement and this fallback serialize
    on the same row lock, so exactly one terminal accounting record wins.
    """

    if deadline_seconds <= 0:
        return False
    job_row = await db.scalar(
        select(PaymentSession)
        .where(PaymentSession.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        job_row is None
        or job_row.protocol != PAID_JOB_PROTOCOL
        or job_row.state != sessions_service.SESSION_STATE_OPEN
    ):
        return False

    opened_at = job_row.opened_at
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=clock.now().tzinfo)
    operational_deadline = opened_at + timedelta(seconds=deadline_seconds)
    if clock.now() < operational_deadline:
        return False

    initial_payment = await db.scalar(
        select(Payment)
        .where(Payment.session_id == job_id)
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    if initial_payment is None:
        return False
    validity_snapshot = await db.scalar(
        select(PaymentDaemonDepositSnapshot)
        .order_by(PaymentDaemonDepositSnapshot.taken_at.desc())
        .limit(1)
    )

    audit = {
        "terminal_kind": "conservative_full_charge",
        "reason": "operational_deadline_without_valid_settlement",
        "job_issued_at": opened_at.isoformat(),
        "operational_deadline": operational_deadline.isoformat(),
        "finalized_at": clock.now().isoformat(),
        "creation_round": initial_payment.creation_round,
        "expires_after_round": initial_payment.expires_after_round,
        "mint_ticket_validity_period": initial_payment.ticket_validity_period,
        "mint_ticket_validity_period_observed_at": (
            initial_payment.ticket_validity_period_observed_at.isoformat()
            if initial_payment.ticket_validity_period_observed_at is not None
            else None
        ),
        "observed_current_round": (
            validity_snapshot.current_round if validity_snapshot is not None else None
        ),
        "current_ticket_validity_period": (
            validity_snapshot.ticket_validity_period if validity_snapshot is not None else None
        ),
        "current_ticket_validity_period_observed_at": (
            validity_snapshot.ticket_validity_period_observed_at.isoformat()
            if validity_snapshot is not None
            and validity_snapshot.ticket_validity_period_observed_at is not None
            else None
        ),
        "evidence": evidence,
    }
    await sessions_service.transition_state(
        db,
        job_id,
        from_state=sessions_service.SESSION_STATE_OPEN,
        to_state=sessions_service.SESSION_STATE_CLOSED,
        clock=clock,
    )
    job_row.actual_units = None
    job_row.billed_value_wei = job_row.funded_value_wei
    job_row.outcome = "conservative_full_charge"
    job_row.breakdown = {**(job_row.breakdown or {}), **audit}
    await db.flush()
    await sessions_service.record_settlement(
        db,
        job_id,
        event_type="conservative_full_charge",
        clock=clock,
        actual_units=None,
        billed_value_wei=job_row.funded_value_wei,
        outcome="conservative_full_charge",
        raw_record=audit,
    )
    return True


async def get_job_status(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
) -> JobStatusResponse:
    """Return billing state while preserving evidence distinctions."""

    job_row = await db.get(PaymentSession, job_id)
    if (
        job_row is None
        or job_row.user_id != user_id
        or job_row.protocol != PAID_JOB_PROTOCOL
        or not job_row.broker_request_id
    ):
        raise JobNotFound
    breakdown = job_row.breakdown or {}
    exchange = breakdown.get("broker_exchange")
    exchange_outcome = exchange.get("outcome") if isinstance(exchange, dict) else None
    if not isinstance(exchange_outcome, str):
        exchange_outcome = None

    accounting_outcome: Literal[
        "unresolved",
        "non_admission_audit",
        "broker_settled",
        "conservative_full_charge",
    ]
    if job_row.outcome == "conservative_full_charge":
        accounting_outcome = "conservative_full_charge"
    elif job_row.state == sessions_service.SESSION_STATE_CLOSED:
        accounting_outcome = "broker_settled"
    elif exchange_outcome == BrokerExchangeOutcome.NOT_ADMITTED.value:
        accounting_outcome = "non_admission_audit"
    else:
        accounting_outcome = "unresolved"

    initial_payment = await db.scalar(
        select(Payment)
        .where(Payment.session_id == job_id)
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    validity_snapshot = await db.scalar(
        select(PaymentDaemonDepositSnapshot)
        .order_by(PaymentDaemonDepositSnapshot.taken_at.desc())
        .limit(1)
    )

    return JobStatusResponse(
        job_id=job_row.id,
        request_id=job_row.broker_request_id,
        work_id=job_row.work_id,
        state=job_row.state,
        accounting_outcome=accounting_outcome,
        broker_exchange_outcome=exchange_outcome,
        actual_units=job_row.actual_units,
        billed_value_wei=(
            int(job_row.billed_value_wei) if job_row.billed_value_wei is not None else None
        ),
        funded_value_wei=int(job_row.funded_value_wei),
        creation_round=(initial_payment.creation_round if initial_payment is not None else None),
        expires_after_round=(
            initial_payment.expires_after_round if initial_payment is not None else None
        ),
        mint_ticket_validity_period=(
            initial_payment.ticket_validity_period if initial_payment is not None else None
        ),
        mint_ticket_validity_period_observed_at=(
            initial_payment.ticket_validity_period_observed_at
            if initial_payment is not None
            else None
        ),
        observed_current_round=(
            validity_snapshot.current_round if validity_snapshot is not None else None
        ),
        current_ticket_validity_period=(
            validity_snapshot.ticket_validity_period if validity_snapshot is not None else None
        ),
        current_ticket_validity_period_observed_at=(
            validity_snapshot.ticket_validity_period_observed_at
            if validity_snapshot is not None
            else None
        ),
        opened_at=job_row.opened_at,
        closed_at=job_row.closed_at,
    )
