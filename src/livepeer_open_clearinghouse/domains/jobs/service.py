"""Business logic for the jobs domain.

Composes the same primitives ``sessions.service`` uses
(``create_session``, ``transition_state``, ``record_settlement``,
``billing.encumber_for_session`` / ``release_session_encumbrance``)
but gated on the authoritative ``paid-job/v1`` protocol.

A job is a short-lived ``payment_session`` row with a job-class protocol.
Opening issues a scoped authorization and replenishes only bounded shortfall
in the shared payer-payee account. Settlement mirrors session close.

  - Protocol gate: ``paid-job/v1`` instead of ``paid-session/v1``.
  - ``max_total_units`` bounds the job authorization and customer hold; it is
    not a wholesale funding target.
  - Response shape uses ``job_id`` naming (just sugar over
    ``session_id``) and exposes a single ``settle_endpoint``
    instead of refill+close pair.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any, Literal, TypedDict

import rfc8785
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.types import CustomerPricingSnapshot
from livepeer_open_clearinghouse.domains.jobs.types import (
    CreateJobResponse,
    JobStatusResponse,
    SettleJobResponse,
    SettlementEnvelope,
)
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.domains.payments.repo import (
    Payment,
    PaymentDaemonDepositSnapshot,
)
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
    SpendAuthorizationGrant,
)
from livepeer_open_clearinghouse.domains.telemetry import server_events as telemetry_events
from livepeer_open_clearinghouse.domains.wholesale import service as wholesale_service
from livepeer_open_clearinghouse.domains.wholesale.types import WholesaleFundingLimits
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    InsufficientCredit,
    NoRouteAvailable,
    NoSettlementDelegation,
    OpenClearinghouseError,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerExchangeOutcome,
    BrokerExchangeResult,
    BrokerSettlementClient,
    BrokerSettlementQueryError,
    BrokerWholesaleAccountClient,
    NonAdmissionQuery,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.providers.payment_daemon import (
    PaymentDaemonClient,
)
from livepeer_open_clearinghouse.providers.registry_daemon import (
    RegistryClient,
    RouteBinding,
    SelectedRoute,
    select_bound_route,
)
from livepeer_open_clearinghouse.providers.settlement_verification import (
    JobSettlementExpectation,
    NonAdmissionExpectation,
    SettlementVerificationError,
    verify_job_settlement,
    verify_non_admission,
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


async def _select_job_route(
    registry: RegistryClient,
    *,
    capability: str,
    offering: str,
    transport: Literal["unary", "stream", "multipart"],
    binding: RouteBinding | None,
) -> SelectedRoute:
    route = await select_bound_route(
        registry,
        capability=capability,
        offering=offering,
        binding=binding,
    )
    if route is None:
        if binding is not None:
            raise sessions_service.RouteBindingMismatch(binding=binding)
        raise NoRouteAvailable(capability=capability, offering=offering)
    if route.protocol != PAID_JOB_PROTOCOL:
        raise ProtocolNotSupportedForJob(protocol=route.protocol)
    job_axes = route.job
    if job_axes is None:  # pragma: no cover - enforced by SelectedRoute validation
        raise RuntimeError("paid-job/v1 route has no job axes")
    if transport not in job_axes.transports:
        raise TransportNotSupportedForJob(
            transport=transport,
            declared=frozenset(job_axes.transports),
        )
    if not route.settlement_keys:
        # A snapshot without delegation pins a job that can never verify a
        # settlement; the funds would sit encumbered until an operator
        # intervenes. Fail closed here instead of at settle time.
        raise NoSettlementDelegation(capability=capability, offering=offering)
    return route


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


async def _open_wholesale_job(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    route: SelectedRoute,
    estimated_units: int,
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
    transport: Literal["unary", "stream", "multipart"],
) -> CreateJobResponse:
    """Create one account-authorized job without exposing funding tickets."""

    job = await sessions_service.claim_wholesale_engagement(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        broker_request_id=request_id,
        estimated_units=estimated_units,
        max_total_units=max_total_units,
        max_debit_wei=max_debit_wei,
        route=route,
        clock=clock,
        sdk_identity=sdk_identity,
        spend_period_seconds=spend_period_seconds,
        spend_period_cap_wei=spend_period_cap_wei,
    )
    now = clock.now()
    authorization_id = f"loc-auth:{request_id}"
    auth_request, auth_response = await payments_service.issue_route_locked_authorization(
        daemon=daemon,
        route=route,
        authorization_id=authorization_id,
        request_id=request_id,
        session_id="",
        request_digest=request_digest,
        caller_public_key=caller_public_key,
        max_debit_wei=max_debit_wei,
        max_total_units=max_total_units,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        chain_id=settings.wholesale_chain_id,
    )
    await sessions_service.record_spend_authorization_grant(
        db,
        engagement_id=job.id,
        user_id=user_id,
        route=route,
        request=auth_request,
        response=auth_response,
    )
    await db.commit()
    payer = "0x" + auth_response.payer.hex()
    observation = await broker.get_wholesale_account(
        broker_url=route.worker_url,
        payer_eth_address=payer,
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
    mint_id = f"loc-account:{request_id}"
    funding = await wholesale_service.claim_account_funding(
        db,
        route=route,
        observation=observation,
        plan=plan,
        limits=limits,
        mint_request_id=mint_id,
        correlation_id=str(job.id),
        protocol_version="wholesale-account/1.0.0-draft",
    )
    await wholesale_service.complete_account_funding(
        db,
        funding=funding,
        route=route,
        observation=observation,
        plan=plan,
        payer_eth_address=payer,
        chain_id=settings.wholesale_chain_id,
        broker=broker,
        daemon=daemon,
        acknowledged_at=clock.now(),
    )
    return CreateJobResponse(
        job_id=job.id,
        request_id=request_id,
        work_id=authorization_id,
        broker_url=route.worker_url,
        protocol=route.protocol,
        transport=transport,
        work_unit=route.work_unit,
        route_snapshot=route.snapshot_view(),
        spend_authorization=auth_response.authorization_b64,
        accounting_mode="wholesale_account",
        expected_value_wei=int(plan.shortfall_wei),
        funded_value_wei=int(plan.shortfall_wei),
        settle_endpoint=_settle_endpoint_for(job.id),
        opened_at=job.opened_at,
    )


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
    route_binding: RouteBinding | None = None,
    request_id: str | None = None,
    workload_request_digest: bytes,
    caller_public_key: bytes,
    broker_wholesale: BrokerWholesaleAccountClient | None = None,
) -> CreateJobResponse:
    """Open a one-shot job with a route-locked wholesale authorization.

    ``max_total_units`` defaults to ``estimated_units``. It bounds both the
    customer hold and broker debit authority, but does not size a job-specific
    payment ticket; funding replenishes the shared payer-payee account.
    """
    broker_request_id = request_id or str(uuid.uuid4())
    effective_max = max_total_units if max_total_units is not None else estimated_units
    if effective_max < estimated_units:
        raise sessions_service.InvalidSessionRequest(
            message="max_total_units must be >= estimated_units"
        )

    cfg = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)

    # Discovery
    route = await _select_job_route(
        registry,
        capability=capability,
        offering=offering,
        transport=transport,
        binding=route_binding,
    )
    if not route.features.wholesale_accounts:
        raise NoRouteAvailable(capability=capability, offering=offering)
    if broker_wholesale is None:
        raise DaemonUnavailable(
            daemon="wholesale-account", reason="broker account client is unavailable"
        )

    # The cumulative ceiling determines customer exposure and authorization
    # scope, independently of aggregate wholesale funding.
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

    assert broker_wholesale is not None
    return await _open_wholesale_job(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        route=route,
        estimated_units=estimated_units,
        max_total_units=effective_max,
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
        transport=transport,
    )


async def settle_job(  # noqa: PLR0912, PLR0915 — explicit settlement state machine
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
    if job_row.accounting_mode != "wholesale_account":
        raise SettlementVerificationFailed(reason="legacy_accounting_disabled")

    snapshot = job_row.route_snapshot or {}
    grant = await db.scalar(
        select(SpendAuthorizationGrant).where(
            SpendAuthorizationGrant.session_id == job_id,
            SpendAuthorizationGrant.authorization_id == job_row.authorization_id,
        )
    )
    if grant is None:
        raise SettlementVerificationFailed(reason="missing_authorization")
    authorization_id = grant.authorization_id
    authorized_value_wei = int(grant.max_debit_wei)
    price_wei = Decimal(str(snapshot.get("price_per_work_unit_wei", "0")))
    expected_work_unit = str(snapshot.get("work_unit", ""))
    if work_unit != expected_work_unit:
        raise WorkUnitMismatch(expected=expected_work_unit, received=work_unit)
    try:
        request_id = job_row.broker_request_id
        if not request_id:
            raise SettlementVerificationError(
                "missing_request_id", "job has no durable broker request id"
            )
        if settlement.signature is None:
            raise SettlementVerificationError(
                "missing_signature", "broker settlement carries no signature"
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
                authorization_id=authorization_id,
                authorized_value_wei=authorized_value_wei,
            ),
        )
    except (KeyError, TypeError, ValueError, SettlementVerificationError) as exc:
        reason = exc.code if isinstance(exc, SettlementVerificationError) else "invalid_snapshot"
        await telemetry_events.emit_settlement_verification_failed(
            db,
            api_key_id=job_row.api_key_id,
            user_id=user_id,
            session_id=job_id,
            protocol=job_row.protocol,
            reason=reason,
            clock=clock,
        )
        raise SettlementVerificationFailed(reason=reason) from exc
    if outcome is not None and outcome != verified.outcome:
        await telemetry_events.emit_settlement_verification_failed(
            db,
            api_key_id=job_row.api_key_id,
            user_id=user_id,
            session_id=job_id,
            protocol=job_row.protocol,
            reason="outcome_mismatch",
            clock=clock,
        )
        raise SettlementVerificationFailed(reason="outcome_mismatch")
    wholesale_billed_value_wei = Decimal(verified.billed_value_wei)
    if job_row.customer_pricing is None or job_row.customer_max_debit_wei is None:
        raise SettlementVerificationFailed(reason="missing_customer_pricing")
    pricing = CustomerPricingSnapshot.model_validate(job_row.customer_pricing)
    billed_value_wei = billing_service.calculate_customer_charge(
        pricing,
        actual_units=verified.actual_units,
        wholesale_debit_wei=wholesale_billed_value_wei,
    )
    if billed_value_wei > job_row.customer_max_debit_wei:
        raise SettlementVerificationFailed(reason="customer_cap_exceeded")
    refund_wei = job_row.customer_max_debit_wei - billed_value_wei

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
        await billing_service.release_customer_engagement(
            db,
            user_id=user_id,
            engagement_id=job_row.id,
            amount_wei=refund_wei,
        )

    # Finalize fields
    final_outcome = verified.outcome
    job_row.actual_units = actual_units
    job_row.billed_value_wei = billed_value_wei
    job_row.outcome = final_outcome
    assert job_row.authorization_id is not None
    await sessions_service.mark_spend_authorization_settled(
        db,
        engagement_id=job_row.id,
        authorization_id=job_row.authorization_id,
        clock=clock,
    )
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


async def reconcile_open_jobs(  # noqa: PLR0912, PLR0915 — explicit outcome state machine
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
                    PaymentSession.accounting_mode == "wholesale_account",
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
        broker_url = snapshot.get("broker_url", snapshot.get("worker_url"))
        if not request_id or not isinstance(broker_url, str) or not broker_url:
            continue
        try:
            exchange = await settlement_client.get_job_exchange(
                broker_url=broker_url,
                request_id=request_id,
            )
        except BrokerSettlementQueryError as exc:
            # A transport or protocol failure is also an unresolved lookup
            # result. Keep retrying before the operator-selected deadline,
            # but do not let an unreachable broker bypass the terminal policy
            # forever. The error is deliberately recorded as LOC observation,
            # never as broker evidence.
            evidence: dict[str, object] = {
                "request_id": request_id,
                "outcome": "LOOKUP_FAILED",
                "detail": str(exc),
            }
            job_reconciliation_observations_total.labels(outcome="LOOKUP_FAILED").inc()
            job_row.last_polled_at = clock.now()
            job_row.breakdown = {
                **(job_row.breakdown or {}),
                "broker_exchange": evidence,
            }
            await db.flush()
            if settings.job_conservative_charge_after_seconds <= 0:
                continue
            charged = await finalize_conservative_full_charge(
                db,
                job_id=job_row.id,
                clock=clock,
                deadline_seconds=settings.job_conservative_charge_after_seconds,
                evidence=evidence,
            )
            if charged:
                job_terminal_accounting_total.labels(terminal_kind="conservative_full_charge").inc()
                finalized += 1
            continue

        if exchange.outcome is BrokerExchangeOutcome.NO_RECORD:
            try:
                exchange = await _request_non_admission(
                    db,
                    settlement_client=settlement_client,
                    job_row=job_row,
                    broker_url=broker_url,
                    request_id=request_id,
                )
            except BrokerSettlementQueryError as exc:
                exchange = exchange.model_copy(
                    update={"detail": f"non-admission query failed: {exc}"}
                )

        job_reconciliation_observations_total.labels(outcome=exchange.outcome.value).inc()
        job_row.last_polled_at = clock.now()
        job_row.breakdown = {
            **(job_row.breakdown or {}),
            "broker_exchange": _exchange_audit_record(exchange),
        }
        await db.flush()
        if exchange.outcome is BrokerExchangeOutcome.NOT_ADMITTED:
            await _retain_verified_non_admission(
                db,
                job_row=job_row,
                exchange=exchange,
                clock=clock,
            )
        settled = False
        if exchange.outcome is BrokerExchangeOutcome.SETTLED:
            claims = _recovered_settlement_claims(exchange)
            if (
                claims is not None
                and exchange.settlement is not None
                and not _settlement_is_blocked(job_row, exchange.settlement)
            ):
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
                except (SettlementVerificationFailed, WorkUnitMismatch) as exc:
                    # Verification of a fixed record against an immutable
                    # snapshot cannot succeed on retry. Remember the record so
                    # the next pass skips it (one failure event, not one per
                    # minute) and the operator attention view shows why.
                    reason = (
                        str(exc.details.get("reason", exc.code))
                        if isinstance(exc, SettlementVerificationFailed)
                        else "work_unit_mismatch"
                    )
                    _record_settlement_block(
                        job_row, exchange.settlement, reason=reason, clock=clock
                    )
                    await db.flush()
                except (JobAlreadySettled, ValueError):
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


async def _request_non_admission(
    db: AsyncSession,
    *,
    settlement_client: BrokerSettlementClient,
    job_row: PaymentSession,
    broker_url: str,
    request_id: str,
) -> BrokerExchangeResult:
    snapshot = job_row.route_snapshot or {}
    payer_scope = await _job_payer_scope(db, job_row)
    if payer_scope is None:
        raise BrokerSettlementQueryError("job lacks persisted payer sender scope")
    sender_eth_address, recipient_eth_address = payer_scope
    try:
        query = NonAdmissionQuery(
            protocol=PAID_JOB_PROTOCOL,
            work_id=job_row.work_id,
            sender=sender_eth_address,
            recipient=recipient_eth_address,
            quote_id=str(snapshot["quote_id"]),
            quote_version=int(snapshot["quote_version"]),
            constraint_fingerprint=str(snapshot["constraint_fingerprint"]),
            route_fingerprint=str(snapshot["route_fingerprint"]),
            job_issued_at=(
                job_row.opened_at
                if job_row.opened_at.tzinfo is not None
                else job_row.opened_at.replace(tzinfo=UTC)
            ).isoformat(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BrokerSettlementQueryError("job lacks valid non-admission scope") from exc
    return await settlement_client.request_non_admission(
        broker_url=broker_url,
        request_id=request_id,
        query=query,
    )


async def _job_payer_scope(db: AsyncSession, job_row: PaymentSession) -> tuple[str, str] | None:
    """Return the immutable wholesale payer/payee scope."""

    grant = await db.scalar(
        select(SpendAuthorizationGrant).where(
            SpendAuthorizationGrant.session_id == job_row.id,
            SpendAuthorizationGrant.authorization_id == job_row.authorization_id,
        )
    )
    snapshot = job_row.route_snapshot or {}
    recipient = snapshot.get("eth_address")
    if grant is None or not isinstance(recipient, str):
        return None
    return grant.payer_eth_address.lower(), recipient.lower()


async def _retain_verified_non_admission(
    db: AsyncSession,
    *,
    job_row: PaymentSession,
    exchange: BrokerExchangeResult,
    clock: Clock,
) -> None:
    """Verify and append one audit claim without changing accounting state."""

    locked_job = await db.scalar(
        select(PaymentSession)
        .where(PaymentSession.id == job_row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_job is None:
        return
    job_row = locked_job
    envelope = exchange.non_admission
    request_id = job_row.broker_request_id
    if envelope is None or request_id is None:
        return
    snapshot = job_row.route_snapshot or {}
    audit = _exchange_audit_record(exchange)
    try:
        payer_scope = await _job_payer_scope(db, job_row)
        if payer_scope is None:
            raise SettlementVerificationError("missing_sender", "payer sender was not persisted")
        sender_eth_address, recipient_eth_address = payer_scope
        settlement_keys = snapshot["settlement_keys"]
        if not isinstance(settlement_keys, list) or not settlement_keys:
            raise SettlementVerificationError(
                "missing_delegation", "route snapshot has no settlement keys"
            )
        verified = verify_non_admission(
            envelope,
            settlement_keys=settlement_keys,
            expected=NonAdmissionExpectation(
                protocol=PAID_JOB_PROTOCOL,
                request_id=request_id,
                work_id=job_row.work_id,
                sender=sessions_service._eth_address_to_bytes(sender_eth_address),
                recipient=sessions_service._eth_address_to_bytes(recipient_eth_address),
                quote_id=str(snapshot["quote_id"]),
                quote_version=int(snapshot["quote_version"]),
                constraint_fingerprint=bytes.fromhex(str(snapshot["constraint_fingerprint"])),
                route_fingerprint=bytes.fromhex(str(snapshot["route_fingerprint"])),
                broker_eth_address=str(snapshot["eth_address"]),
                job_issued_at=(
                    job_row.opened_at
                    if job_row.opened_at.tzinfo is not None
                    else job_row.opened_at.replace(tzinfo=UTC)
                ),
            ),
        )
    except (KeyError, TypeError, ValueError, SettlementVerificationError) as exc:
        audit["verification"] = {
            "status": "rejected",
            "reason": exc.code
            if isinstance(exc, SettlementVerificationError)
            else "invalid_snapshot",
        }
        job_row.breakdown = {**(job_row.breakdown or {}), "broker_exchange": audit}
        await db.flush()
        return

    evidence_digest = hashlib.sha256(rfc8785.dumps(envelope)).hexdigest()
    audit["verification"] = {
        "status": "verified",
        "evidence_digest": evidence_digest,
        "signing_public_key": verified.signing_public_key,
        "observed_at": verified.observed_at.isoformat(),
        "coverage_started_at": verified.coverage_started_at.isoformat(),
    }
    job_row.breakdown = {**(job_row.breakdown or {}), "broker_exchange": audit}
    prior = list(
        (
            await db.scalars(
                select(PaymentSettlement).where(
                    PaymentSettlement.session_id == job_row.id,
                    PaymentSettlement.event_type == "non_admission_audit",
                )
            )
        ).all()
    )
    if not any(
        isinstance(row.raw_record, dict)
        and row.raw_record.get("evidence_digest") == evidence_digest
        for row in prior
    ):
        await sessions_service.record_settlement(
            db,
            job_row.id,
            event_type="non_admission_audit",
            clock=clock,
            outcome="NOT_ADMITTED",
            raw_record={"evidence_digest": evidence_digest, **audit},
        )
    await db.flush()


def _settlement_signature(settlement: dict[str, Any]) -> str | None:
    signature = settlement.get("signature")
    if isinstance(signature, dict):
        value = signature.get("value")
        if isinstance(value, str):
            return value
    return None


def _settlement_is_blocked(job_row: PaymentSession, settlement: dict[str, Any]) -> bool:
    """True when this exact broker record already failed verification."""
    block = (job_row.breakdown or {}).get("settlement_block")
    if not isinstance(block, dict):
        return False
    return block.get("signature") == _settlement_signature(settlement)


def _record_settlement_block(
    job_row: PaymentSession, settlement: dict[str, Any], *, reason: str, clock: Clock
) -> None:
    existing = (job_row.breakdown or {}).get("settlement_block")
    attempts = int(existing.get("attempts", 0)) + 1 if isinstance(existing, dict) else 1
    first_seen = (
        existing.get("first_seen")
        if isinstance(existing, dict)
        and existing.get("signature") == _settlement_signature(settlement)
        else clock.now().isoformat()
    )
    job_row.breakdown = {
        **(job_row.breakdown or {}),
        "settlement_block": {
            "reason": reason,
            "signature": _settlement_signature(settlement),
            "first_seen": first_seen,
            "last_seen": clock.now().isoformat(),
            "attempts": attempts,
        },
    }


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
    elif (
        exchange_outcome == BrokerExchangeOutcome.NOT_ADMITTED.value
        and isinstance(exchange, dict)
        and isinstance(exchange.get("verification"), dict)
        and exchange["verification"].get("status") == "verified"
    ):
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
