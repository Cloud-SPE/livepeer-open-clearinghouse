"""Usage reads over settled payment sessions.

Every function here is a pure read. The source of truth is the
``payment_session`` table (jobs and sessions share it): what ran, on which
key, how many units, what was funded and what was billed. Money figures
come from the billing domain so the dashboard never disagrees with the
ledger.

Aggregation happens in Python over the rows of the requested window. At
pilot scale (thousands of rows per window) that is simpler and more
portable than dialect-specific date bucketing; revisit with a materialized
daily rollup once a window regularly exceeds ~100k rows.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession, PaymentSettlement
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage.types import (
    AccountingOutcome,
    ApiKeyUsage,
    AttentionCounts,
    DayUsage,
    OfferingUsage,
    SettlementFailure,
    UnresolvedJob,
    UnterminatedSession,
    UsageAttention,
    UsageJobPage,
    UsageJobView,
    UsageOverview,
    UsagePeriod,
    UsageSummary,
    UsageTotals,
    UserUsage,
    ZeroOutputSession,
)
from livepeer_open_clearinghouse.providers.clock import Clock
from livepeer_open_clearinghouse.settings import Settings

STATE_OPEN = "open"
STATE_DRAINING = "draining"
STATE_CLOSED = "closed"
OPEN_STATES = (STATE_OPEN, STATE_DRAINING)

#: An open job older than this is "unresolved": the SDK never settled and
#: the reconciler has not recovered it yet. Mirrors the default job
#: reconciliation cadence with headroom for slow broker work.
DEFAULT_STALE_AFTER_SECONDS = 900

# Session silence is not financial evidence. After this much time without a
# successful LOC funding event, surface the row for operator investigation but
# retain its hold until the broker supplies a signed terminal settlement.
DEFAULT_SESSION_ATTENTION_AFTER_SECONDS = 2 * 60 * 60
ZERO_OUTPUT_MIN_DURATION_SECONDS = 60
ZERO_OUTPUT_LOOKBACK_HOURS = 24

SETTLEMENT_FAILURE_EVENT = "server.settlement_verification_failed"

DEFAULT_SUMMARY_DAYS = 30
MAX_PAGE_LIMIT = 500


@dataclass(frozen=True, slots=True)
class JobFilters:
    capability: str | None = None
    offering: str | None = None
    api_key_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    state: str | None = None  # "open" (open or draining) | "closed"
    since: datetime | None = None
    until: datetime | None = None


# ---------------------------------------------------------------------------
# Time helpers — rows store naive UTC; the wire is tz-aware UTC.
# ---------------------------------------------------------------------------


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def _default_window(
    clock: Clock, since: datetime | None, until: datetime | None
) -> tuple[datetime, datetime]:
    now = clock.now()
    end = _aware(until) if until is not None else now
    start = _aware(since) if since is not None else end - timedelta(days=DEFAULT_SUMMARY_DAYS)
    if start > end:
        start, end = end, start
    return start, end


# ---------------------------------------------------------------------------
# Row -> view
# ---------------------------------------------------------------------------


def accounting_outcome_for(
    row: PaymentSession, *, now: datetime, stale_after_seconds: int
) -> AccountingOutcome:
    if row.state == STATE_CLOSED:
        if row.outcome == "conservative_full_charge":
            return "conservative_full_charge"
        return "broker_settled"
    if blocked_reason_for(row) is not None:
        return "unresolved"
    age = (_naive(now) - _naive(row.opened_at)).total_seconds()
    return "unresolved" if age > stale_after_seconds else "open"


def reported_units_for(row: PaymentSession) -> int | None:
    """Units the broker reported for this row through the exchange lookup, if any.

    This is broker-asserted, not verified: it tells an operator what
    ``accept_reported`` would bill, nothing more.
    """
    exchange = (row.breakdown or {}).get("broker_exchange")
    if not isinstance(exchange, dict):
        return None
    units = exchange.get("work_units")
    if isinstance(units, int) and not isinstance(units, bool) and units >= 0:
        return units
    settlement = exchange.get("settlement")
    payload = settlement.get("payload") if isinstance(settlement, dict) else None
    if isinstance(payload, dict):
        for key in ("actual_units", "debited_units"):
            try:
                value = int(payload[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return None


def blocked_reason_for(row: PaymentSession) -> str | None:
    """Why the reconciler stopped trying to settle this row, if it did."""
    block = (row.breakdown or {}).get("settlement_block")
    if isinstance(block, dict):
        reason = block.get("reason")
        if isinstance(reason, str):
            return reason
    return None


def _work_unit(row: PaymentSession) -> str:
    snapshot = row.route_snapshot or {}
    unit = snapshot.get("work_unit")
    return unit if isinstance(unit, str) and unit else "units"


def _held(row: PaymentSession) -> Decimal:
    return Decimal(row.funded_value_wei) if row.state in OPEN_STATES else Decimal(0)


def _refunded(row: PaymentSession) -> Decimal | None:
    if row.state != STATE_CLOSED or row.billed_value_wei is None:
        return None
    refund = Decimal(row.funded_value_wei) - Decimal(row.billed_value_wei)
    return refund if refund > 0 else Decimal(0)


def job_view(
    row: PaymentSession,
    *,
    now: datetime,
    stale_after_seconds: int,
    api_key_label: str | None,
    user_email: str | None = None,
    include_user: bool = False,
) -> UsageJobView:
    closed_at = _aware(row.closed_at) if row.closed_at is not None else None
    opened_at = _aware(row.opened_at)
    duration = (closed_at - opened_at).total_seconds() if closed_at is not None else None
    return UsageJobView(
        id=row.id,
        protocol=row.protocol,
        capability=row.capability,
        offering=row.offering,
        api_key_id=row.api_key_id,
        api_key_label=api_key_label,
        sdk_identity=row.sdk_identity,
        state=row.state,
        accounting_outcome=accounting_outcome_for(
            row, now=now, stale_after_seconds=stale_after_seconds
        ),
        work_unit=_work_unit(row),
        estimated_units=row.estimated_units,
        max_total_units=row.max_total_units,
        actual_units=row.actual_units,
        funded_value_wei=Decimal(row.funded_value_wei),
        billed_value_wei=Decimal(row.billed_value_wei)
        if row.billed_value_wei is not None
        else None,
        refunded_wei=_refunded(row),
        held_wei=_held(row),
        opened_at=opened_at,
        closed_at=closed_at,
        duration_seconds=duration,
        blocked_reason=blocked_reason_for(row) if row.state in OPEN_STATES else None,
        reported_units=reported_units_for(row) if row.state in OPEN_STATES else None,
        user_id=row.user_id if include_user else None,
        user_email=user_email if include_user else None,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _apply_filters(stmt: Any, filters: JobFilters) -> Any:
    if filters.user_id is not None:
        stmt = stmt.where(PaymentSession.user_id == filters.user_id)
    if filters.capability:
        stmt = stmt.where(PaymentSession.capability == filters.capability)
    if filters.offering:
        stmt = stmt.where(PaymentSession.offering == filters.offering)
    if filters.api_key_id is not None:
        stmt = stmt.where(PaymentSession.api_key_id == filters.api_key_id)
    if filters.state == "closed":
        stmt = stmt.where(PaymentSession.state == STATE_CLOSED)
    elif filters.state == "open":
        stmt = stmt.where(PaymentSession.state.in_(OPEN_STATES))
    if filters.since is not None:
        stmt = stmt.where(PaymentSession.opened_at >= _naive(filters.since))
    if filters.until is not None:
        stmt = stmt.where(PaymentSession.opened_at < _naive(filters.until))
    return stmt


async def _labels_for(db: AsyncSession, key_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not key_ids:
        return {}
    rows = await db.execute(select(ApiKey.id, ApiKey.label).where(ApiKey.id.in_(key_ids)))
    return {key_id: label for key_id, label in rows.all()}


async def _emails_for(db: AsyncSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
    return {user_id: email for user_id, email in rows.all()}


async def list_jobs(
    db: AsyncSession,
    *,
    filters: JobFilters,
    limit: int,
    offset: int,
    clock: Clock,
    include_user: bool = False,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> UsageJobPage:
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    offset = max(0, offset)
    base = _apply_filters(select(PaymentSession), filters)
    total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        (
            await db.scalars(
                base.order_by(PaymentSession.opened_at.desc(), PaymentSession.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    labels = await _labels_for(db, {r.api_key_id for r in rows})
    emails = await _emails_for(db, {r.user_id for r in rows}) if include_user else {}
    now = clock.now()
    return UsageJobPage(
        items=[
            job_view(
                r,
                now=now,
                stale_after_seconds=stale_after_seconds,
                api_key_label=labels.get(r.api_key_id),
                user_email=emails.get(r.user_id),
                include_user=include_user,
            )
            for r in rows
        ],
        total=int(total),
        limit=limit,
        offset=offset,
    )


def _day_key(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%d")


def _by_day(rows: list[PaymentSession]) -> list[DayUsage]:
    """Billed value bucketed by the UTC day the job closed (open jobs by open day, unbilled)."""
    jobs: dict[str, int] = defaultdict(int)
    billed: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for r in rows:
        stamp = r.closed_at if r.closed_at is not None else r.opened_at
        key = _day_key(stamp)
        jobs[key] += 1
        if r.billed_value_wei is not None:
            billed[key] += Decimal(r.billed_value_wei)
    return [DayUsage(day=d, jobs=jobs[d], billed_wei=billed[d]) for d in sorted(jobs)]


async def summarize(
    db: AsyncSession,
    *,
    clock: Clock,
    user_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
    include_users: bool = False,
) -> UsageSummary:
    start, end = _default_window(clock, since, until)
    rows = list(
        (
            await db.scalars(
                _apply_filters(
                    select(PaymentSession),
                    JobFilters(user_id=user_id, since=start, until=end),
                ).order_by(PaymentSession.opened_at.asc())
            )
        ).all()
    )
    totals = UsageTotals(
        jobs=len(rows),
        open_jobs=sum(1 for r in rows if r.state in OPEN_STATES),
        closed_jobs=sum(1 for r in rows if r.state == STATE_CLOSED),
        billed_wei=sum(
            (Decimal(r.billed_value_wei) for r in rows if r.billed_value_wei is not None),
            Decimal(0),
        ),
        refunded_wei=sum((_refunded(r) or Decimal(0) for r in rows), Decimal(0)),
        held_wei=sum((_held(r) for r in rows), Decimal(0)),
    )

    offerings: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r.capability, r.offering)
        agg = offerings.setdefault(
            key,
            {
                "protocol": r.protocol,
                "work_unit": _work_unit(r),
                "jobs": 0,
                "units": 0,
                "billed": Decimal(0),
                "held": Decimal(0),
            },
        )
        agg["jobs"] += 1
        agg["units"] += int(r.actual_units or 0)
        if r.billed_value_wei is not None:
            agg["billed"] += Decimal(r.billed_value_wei)
        agg["held"] += _held(r)
    by_offering = sorted(
        (
            OfferingUsage(
                capability=cap,
                offering=off,
                protocol=a["protocol"],
                work_unit=a["work_unit"],
                jobs=a["jobs"],
                units=a["units"],
                billed_wei=a["billed"],
                held_wei=a["held"],
            )
            for (cap, off), a in offerings.items()
        ),
        key=lambda o: (-o.billed_wei, o.capability, o.offering),
    )

    labels = await _labels_for(db, {r.api_key_id for r in rows})
    keys: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        agg = keys.setdefault(r.api_key_id, {"jobs": 0, "billed": Decimal(0)})
        agg["jobs"] += 1
        if r.billed_value_wei is not None:
            agg["billed"] += Decimal(r.billed_value_wei)
    by_api_key = sorted(
        (
            ApiKeyUsage(api_key_id=k, label=labels.get(k), jobs=a["jobs"], billed_wei=a["billed"])
            for k, a in keys.items()
        ),
        key=lambda k: (-k.billed_wei, str(k.api_key_id)),
    )

    by_user: list[UserUsage] | None = None
    if include_users:
        emails = await _emails_for(db, {r.user_id for r in rows})
        users: dict[uuid.UUID, dict[str, Any]] = {}
        for r in rows:
            agg = users.setdefault(r.user_id, {"jobs": 0, "billed": Decimal(0), "held": Decimal(0)})
            agg["jobs"] += 1
            if r.billed_value_wei is not None:
                agg["billed"] += Decimal(r.billed_value_wei)
            agg["held"] += _held(r)
        by_user = sorted(
            (
                UserUsage(
                    user_id=u,
                    email=emails.get(u),
                    jobs=a["jobs"],
                    billed_wei=a["billed"],
                    held_wei=a["held"],
                )
                for u, a in users.items()
            ),
            key=lambda u: (-u.billed_wei, str(u.user_id)),
        )

    return UsageSummary(
        since=start,
        until=end,
        totals=totals,
        by_offering=by_offering,
        by_api_key=by_api_key,
        by_day=_by_day(rows),
        by_user=by_user,
    )


async def overview(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    clock: Clock,
    settings: Settings,
) -> UsageOverview:
    now = clock.now()
    balance = await billing_service.get_balance(db, user_id=user_id)
    config = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)
    period_seconds = max(1, int(config.spend_period_seconds))
    start, end = billing_service.window_bounds_for(now, period_seconds)
    cap = Decimal(config.spend_period_cap_wei) if config.spend_period_cap_wei > 0 else None

    held = await db.scalar(
        select(func.coalesce(func.sum(PaymentSession.funded_value_wei), 0)).where(
            PaymentSession.user_id == user_id, PaymentSession.state.in_(OPEN_STATES)
        )
    )
    open_jobs = await db.scalar(
        select(func.count()).where(
            PaymentSession.user_id == user_id, PaymentSession.state.in_(OPEN_STATES)
        )
    )
    since_30d = now - timedelta(days=DEFAULT_SUMMARY_DAYS)
    closed_rows = list(
        (
            await db.scalars(
                select(PaymentSession).where(
                    PaymentSession.user_id == user_id,
                    PaymentSession.state == STATE_CLOSED,
                    PaymentSession.closed_at >= _naive(since_30d),
                )
            )
        ).all()
    )
    spent_30d = sum(
        (Decimal(r.billed_value_wei) for r in closed_rows if r.billed_value_wei is not None),
        Decimal(0),
    )
    # Spent this period comes from the same settled rows as everything else,
    # not from the spend_window ledger, which is only written while a cap is
    # being enforced. The cap comparison is still against the billing
    # window bounds so it matches what enforcement will apply.
    window_start, window_end = _naive(start), _naive(end)
    spent_period = sum(
        (
            Decimal(r.billed_value_wei)
            for r in closed_rows
            if r.billed_value_wei is not None
            and r.closed_at is not None
            and window_start <= _naive(r.closed_at) < window_end
        ),
        Decimal(0),
    )
    pct = float(spent_period / cap) if cap else None
    return UsageOverview(
        available_wei=Decimal(balance.amount_wei),
        held_wei=Decimal(held or 0),
        spent_period_wei=spent_period,
        spent_30d_wei=spent_30d,
        open_jobs=int(open_jobs or 0),
        period=UsagePeriod(start=start, end=end, seconds=period_seconds, cap_wei=cap, pct_used=pct),
        by_day=_by_day(closed_rows),
    )


async def attention(
    db: AsyncSession,
    *,
    clock: Clock,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    session_attention_after_seconds: int = DEFAULT_SESSION_ATTENTION_AFTER_SECONDS,
    limit: int = 100,
) -> UsageAttention:
    now = clock.now()
    cutoff = _naive(now - timedelta(seconds=stale_after_seconds))
    stale_rows = list(
        (
            await db.scalars(
                select(PaymentSession)
                .where(
                    PaymentSession.protocol == "paid-job/v1",
                    PaymentSession.state.in_(OPEN_STATES),
                    PaymentSession.opened_at < cutoff,
                )
                .order_by(PaymentSession.opened_at.asc())
                .limit(limit)
            )
        ).all()
    )
    unresolved_total = await db.scalar(
        select(func.count()).where(
            PaymentSession.protocol == "paid-job/v1",
            PaymentSession.state.in_(OPEN_STATES),
            PaymentSession.opened_at < cutoff,
        )
    )

    zero_output_since = _naive(now - timedelta(hours=ZERO_OUTPUT_LOOKBACK_HOURS))
    zero_output_candidates = list(
        (
            await db.scalars(
                select(PaymentSession)
                .where(
                    PaymentSession.protocol == "paid-session/v1",
                    PaymentSession.state == STATE_CLOSED,
                    PaymentSession.actual_units == 0,
                    PaymentSession.closed_at.is_not(None),
                    PaymentSession.closed_at >= zero_output_since,
                )
                .order_by(PaymentSession.closed_at.desc())
            )
        ).all()
    )
    zero_output_rows = []
    for row in zero_output_candidates:
        if row.closed_at is None:
            continue
        diagnostics = (row.breakdown or {}).get("broker_diagnostics")
        termination_reason = (
            diagnostics.get("termination_reason") if isinstance(diagnostics, dict) else None
        )
        duration = (_naive(row.closed_at) - _naive(row.opened_at)).total_seconds()
        if duration >= ZERO_OUTPUT_MIN_DURATION_SECONDS or termination_reason == "output_failed":
            zero_output_rows.append(row)

    last_refill = (
        select(
            PaymentSettlement.session_id.label("session_id"),
            func.max(PaymentSettlement.recorded_at).label("last_funded_at"),
        )
        .where(PaymentSettlement.event_type == "refill_granted")
        .group_by(PaymentSettlement.session_id)
        .subquery()
    )
    session_rows = (
        await db.execute(
            select(PaymentSession, last_refill.c.last_funded_at)
            .outerjoin(last_refill, last_refill.c.session_id == PaymentSession.id)
            .where(
                PaymentSession.protocol == "paid-session/v1",
                PaymentSession.state.in_(OPEN_STATES),
            )
            .order_by(PaymentSession.opened_at.asc())
        )
    ).all()
    unterminated_candidates: list[tuple[PaymentSession, datetime, datetime]] = []
    for row, last_refill_at in session_rows:
        last_funded_at = last_refill_at or row.opened_at
        attention_after = _naive(last_funded_at) + timedelta(
            seconds=session_attention_after_seconds
        )
        if attention_after < _naive(now):
            unterminated_candidates.append((row, last_funded_at, attention_after))
    failures_since = _naive(now - timedelta(hours=24))
    failure_rows = list(
        (
            await db.scalars(
                select(TelemetryEvent)
                .where(
                    TelemetryEvent.event_type == SETTLEMENT_FAILURE_EVENT,
                    TelemetryEvent.received_ts >= failures_since,
                )
                .order_by(TelemetryEvent.received_ts.desc())
                .limit(limit)
            )
        ).all()
    )
    failures_total = await db.scalar(
        select(func.count()).where(
            TelemetryEvent.event_type == SETTLEMENT_FAILURE_EVENT,
            TelemetryEvent.received_ts >= failures_since,
        )
    )
    emails = await _emails_for(
        db,
        {r.user_id for r in stale_rows}
        | {r.user_id for r in zero_output_rows}
        | {r.user_id for r, _, _ in unterminated_candidates}
        | {e.user_id for e in failure_rows if e.user_id is not None},
    )
    unresolved = [
        UnresolvedJob(
            job_id=r.id,
            user_id=r.user_id,
            user_email=emails.get(r.user_id),
            protocol=r.protocol,
            capability=r.capability,
            offering=r.offering,
            funded_value_wei=Decimal(r.funded_value_wei),
            opened_at=_aware(r.opened_at),
            age_seconds=(_naive(now) - _naive(r.opened_at)).total_seconds(),
            blocked_reason=blocked_reason_for(r),
            reported_units=reported_units_for(r),
        )
        for r in stale_rows
    ]
    failures = [
        SettlementFailure(
            at=_aware(e.received_ts),
            user_id=e.user_id,
            user_email=emails.get(e.user_id) if e.user_id is not None else None,
            protocol=str(e.payload.get("protocol")) if e.payload.get("protocol") else None,
            session_id=str(e.payload.get("session_id")) if e.payload.get("session_id") else None,
            code=str(e.payload.get("code") or "settlement_verification_failed"),
            reason=str(e.payload.get("reason")) if e.payload.get("reason") else None,
        )
        for e in failure_rows
    ]
    zero_output = []
    for row in zero_output_rows[:limit]:
        if row.closed_at is None:
            continue
        diagnostics = (row.breakdown or {}).get("broker_diagnostics")
        safe = diagnostics if isinstance(diagnostics, dict) else {}
        output_state_since = safe.get("output_state_since")
        zero_output.append(
            ZeroOutputSession(
                session_id=row.id,
                user_id=row.user_id,
                user_email=emails.get(row.user_id),
                capability=row.capability,
                offering=row.offering,
                broker_session_id=row.broker_session_id,
                termination_reason=safe.get("termination_reason"),
                output_state=safe.get("output_state"),
                output_state_since=(
                    datetime.fromisoformat(output_state_since)
                    if isinstance(output_state_since, str)
                    else None
                ),
                last_failure_code=safe.get("last_failure_code"),
                duration_seconds=(_naive(row.closed_at) - _naive(row.opened_at)).total_seconds(),
                closed_at=_aware(row.closed_at),
            )
        )
    unterminated = [
        UnterminatedSession(
            session_id=row.id,
            user_id=row.user_id,
            user_email=emails.get(row.user_id),
            capability=row.capability,
            offering=row.offering,
            broker_session_id=row.broker_session_id,
            opened_at=_aware(row.opened_at),
            last_funded_at=_aware(last_funded_at),
            attention_after=_aware(attention_after),
            overdue_seconds=(_naive(now) - attention_after).total_seconds(),
        )
        for row, last_funded_at, attention_after in unterminated_candidates[:limit]
    ]
    return UsageAttention(
        counts=AttentionCounts(
            unresolved=int(unresolved_total or 0),
            settlement_failures_24h=int(failures_total or 0),
            zero_output_sessions=len(zero_output_rows),
            unterminated_sessions=len(unterminated_candidates),
        ),
        unresolved=unresolved,
        settlement_failures=failures,
        zero_output_sessions=zero_output,
        unterminated_sessions=unterminated,
    )
