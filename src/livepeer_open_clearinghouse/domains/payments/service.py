"""Payment-row reads + scheduled maintenance.

Post-exec-plan-002, the headline mint orchestration moved to
``domains/jobs/service.py`` and ``domains/sessions/service.py``
under the handoff-mode design. What remains here:

  - ``list_payments_for_user`` / ``get_payment_by_work_id`` —
    customer-facing read surface (powers ``GET /v1/payments/me``
    and ``GET /v1/payments/{work_id}``).
  - durable claim, replay, and expiry for idempotent job/session creates.
    The claim is committed before calling the payer daemon so a crash cannot
    erase the evidence that a mint may have occurred.
  - ``snapshot_deposit`` / ``list_deposit_snapshots`` — periodic
    capture of the daemon's TicketBroker deposit/reserve state
    for operator observability.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.payments.repo import (
    Payment,
    PaymentDaemonDepositSnapshot,
    PaymentIdempotencyKey,
)
from livepeer_open_clearinghouse.providers.clock import Clock


@dataclass(frozen=True, slots=True)
class CreateRequestClaim:
    """Result of durably claiming a create endpoint idempotency key."""

    broker_request_id: str
    replay_status: int | None = None
    replay_payload: dict[str, Any] | None = None

    @property
    def is_replay(self) -> bool:
        return self.replay_status is not None


def create_request_fingerprint(*, operation: str, payload: dict[str, Any]) -> str:
    """Hash the endpoint semantic input using deterministic JSON."""

    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def claim_create_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    request_fingerprint: str,
    clock: Clock,
    inflight_timeout_seconds: int,
) -> CreateRequestClaim:
    """Commit an in-flight claim before any payer-side effect occurs.

    The explicit commit is a safety barrier. A concurrent insert blocks on
    the primary key, then observes the winner after its save attempt loses.
    """

    row = await _get_create_request(
        session,
        user_id=user_id,
        operation=operation,
        idempotency_key=idempotency_key,
    )
    if row is not None:
        return _claim_from_existing(row, request_fingerprint=request_fingerprint)

    row = PaymentIdempotencyKey(
        user_id=user_id,
        api_key_id=api_key_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        broker_request_id=str(uuid.uuid4()),
        status="in_flight",
        http_status=None,
        response_payload=None,
        payment_id=None,
        expires_at=clock.now() + timedelta(seconds=inflight_timeout_seconds),
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner = await _get_create_request(
            session,
            user_id=user_id,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        if winner is None:  # pragma: no cover - defensive DB anomaly
            raise
        return _claim_from_existing(winner, request_fingerprint=request_fingerprint)
    return CreateRequestClaim(broker_request_id=row.broker_request_id)


async def complete_create_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    http_status: int,
    response_payload: dict[str, Any],
    clock: Clock,
    retention_seconds: int,
    payment_id: uuid.UUID | None = None,
) -> None:
    """Record success in the same transaction as the business mutation."""

    row = await _require_create_request(session, user_id, operation, idempotency_key)
    row.status = "completed"
    row.http_status = http_status
    row.response_payload = response_payload
    row.payment_id = payment_id
    row.expires_at = clock.now() + timedelta(seconds=retention_seconds)
    await session.flush()


async def fail_create_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    http_status: int,
    response_payload: dict[str, Any],
    clock: Clock,
    retention_seconds: int,
) -> None:
    """Persist a deterministic failure after rolling back business writes."""

    row = await _require_create_request(session, user_id, operation, idempotency_key)
    row.status = "failed"
    row.http_status = http_status
    row.response_payload = response_payload
    row.expires_at = clock.now() + timedelta(seconds=retention_seconds)
    await session.commit()


async def _get_create_request(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
) -> PaymentIdempotencyKey | None:
    return await session.get(
        PaymentIdempotencyKey,
        {
            "user_id": user_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
        },
    )


async def _require_create_request(
    session: AsyncSession,
    user_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
) -> PaymentIdempotencyKey:
    row = await _get_create_request(
        session,
        user_id=user_id,
        operation=operation,
        idempotency_key=idempotency_key,
    )
    if row is None:  # pragma: no cover - indicates a deleted durable claim
        raise RuntimeError("idempotency claim disappeared")
    return row


def _claim_from_existing(
    row: PaymentIdempotencyKey, *, request_fingerprint: str
) -> CreateRequestClaim:
    from livepeer_open_clearinghouse.errors import (  # noqa: PLC0415
        IdempotencyInProgress,
        IdempotencyKeyReuse,
        IdempotencyOutcomeUnknown,
    )

    if row.request_fingerprint != request_fingerprint:
        raise IdempotencyKeyReuse
    if row.status == "in_flight":
        raise IdempotencyInProgress
    if row.status == "expired":
        raise IdempotencyOutcomeUnknown
    if row.status not in {"completed", "failed"} or row.http_status is None:
        raise IdempotencyOutcomeUnknown
    return CreateRequestClaim(
        broker_request_id=row.broker_request_id,
        replay_status=row.http_status,
        replay_payload=row.response_payload or {},
    )


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
    result: Payment | None = await session.scalar(
        select(Payment).where(
            Payment.user_id == user_id,
            Payment.work_id == work_id,
        )
    )
    return result


# ---------------------------------------------------------------------------
# Background maintenance (scheduler-driven)
# ---------------------------------------------------------------------------


async def expire_stale_idempotency_keys(session: AsyncSession, *, clock: Clock) -> int:
    """Mark in-flight idempotency-key rows past their TTL as expired.

    Returns the number of rows mutated. Run periodically by APScheduler.

    Terminal results are deleted after their replay-retention window. An
    expired in-flight result is retained because its payer outcome is unknown;
    silently reclaiming it could mint twice.
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
    deleted = await session.execute(
        delete(PaymentIdempotencyKey).where(
            PaymentIdempotencyKey.status.in_(("completed", "failed")),
            PaymentIdempotencyKey.expires_at < now,
        )
    )
    count += int(deleted.rowcount or 0)  # type: ignore[attr-defined]
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
