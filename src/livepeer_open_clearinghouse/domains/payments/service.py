"""Payment-row reads + scheduled maintenance.

Post-exec-plan-002, the headline mint orchestration moved to
``domains/jobs/service.py`` and ``domains/sessions/service.py``
under the handoff-mode design. What remains here:

  - ``list_payments_for_user`` / ``get_payment_by_work_id`` —
    customer-facing read surface (powers ``GET /v1/payments/me``
    and ``GET /v1/payments/{work_id}``).
  - ``expire_stale_idempotency_keys`` — periodic janitor that
    times out abandoned idempotency-key rows. Idempotency is
    no longer actively used (the new endpoints don't accept the
    header), but the table + janitor remain to drain any
    pre-existing in-flight rows cleanly.
  - ``snapshot_deposit`` / ``list_deposit_snapshots`` — periodic
    capture of the daemon's TicketBroker deposit/reserve state
    for operator observability.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.payments.repo import (
    Payment,
    PaymentDaemonDepositSnapshot,
    PaymentIdempotencyKey,
)
from livepeer_open_clearinghouse.providers.clock import Clock


async def list_payments_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = 50,
) -> list[Payment]:
    """Return the user's most-recent payments, newest first."""
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def get_payment_by_work_id(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    work_id: str,
) -> Payment | None:
    """Look up a single payment by its work_id (scoped to user)."""
    return await session.scalar(
        select(Payment).where(
            Payment.user_id == user_id,
            Payment.work_id == work_id,
        )
    )


# ---------------------------------------------------------------------------
# Background maintenance (scheduler-driven)
# ---------------------------------------------------------------------------


async def expire_stale_idempotency_keys(session: AsyncSession, *, clock: Clock) -> int:
    """Mark in-flight idempotency-key rows past their TTL as expired.

    Returns the number of rows mutated. Run periodically by APScheduler.

    The endpoints that wrote to this table were removed under exec-plan
    002's legacy cleanup. The janitor remains to drain any pre-existing
    in-flight rows; expect zero-row results in steady state.
    """
    now: datetime = clock.now()
    rows = await session.scalars(
        select(PaymentIdempotencyKey).where(
            PaymentIdempotencyKey.status == "in_flight",
            PaymentIdempotencyKey.expires_at < now,
        )
    )
    count = 0
    for row in rows:
        row.status = "expired"
        count += 1
    return count


async def snapshot_deposit(
    session: AsyncSession,
    *,
    clock: Clock,
    daemon: object,  # PaymentDaemonClient — typed object to avoid circular import
) -> PaymentDaemonDepositSnapshot:
    """Capture the daemon's TicketBroker deposit/reserve state.

    Run periodically by APScheduler. The resulting rows drive operator
    observability for the on-chain pool drawdown over time.
    """
    info = await daemon.get_deposit_info()  # type: ignore[attr-defined]
    row = PaymentDaemonDepositSnapshot(
        taken_at=clock.now(),
        deposit_wei=Decimal(info.deposit_wei),
        reserve_wei=Decimal(info.reserve_wei),
        withdraw_round=int(info.withdraw_round),
    )
    session.add(row)
    await session.flush()
    return row


async def list_deposit_snapshots(
    session: AsyncSession, *, limit: int = 100
) -> list[PaymentDaemonDepositSnapshot]:
    """Most-recent-first list of deposit snapshots for the admin view."""
    rows = await session.scalars(
        select(PaymentDaemonDepositSnapshot)
        .order_by(PaymentDaemonDepositSnapshot.taken_at.desc())
        .limit(limit)
    )
    return list(rows)
