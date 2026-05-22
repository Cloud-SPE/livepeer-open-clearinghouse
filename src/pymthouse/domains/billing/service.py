"""Business logic for billing.

The ledger is the source of truth; ``credit_balance.amount_wei`` is a
denormalized mirror used for fast reads and ``SELECT ... FOR UPDATE``
locking. Every balance change goes through ``_apply_delta`` which
inserts a ledger row and increments the balance's version.

See docs/RELIABILITY.md.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.billing.repo import (
    CreditBalance,
    CreditLedger,
    CreditTopup,
)
from pymthouse.errors import InsufficientCredit


async def _ensure_balance_row(
    session: AsyncSession, *, user_id: uuid.UUID
) -> CreditBalance:
    """Get-or-create the user's CreditBalance row. Returns a locked row.

    Callers should already be inside a transaction. The returned row is
    held with `FOR UPDATE` so subsequent reads/writes see consistent state.
    """
    row = await session.scalar(
        select(CreditBalance)
        .where(CreditBalance.user_id == user_id)
        .with_for_update()
    )
    if row is None:
        row = CreditBalance(user_id=user_id, amount_wei=Decimal(0), version=0)
        session.add(row)
        await session.flush()
        # Re-fetch with the lock now that the row exists.
        row = await session.scalar(
            select(CreditBalance)
            .where(CreditBalance.user_id == user_id)
            .with_for_update()
        )
        assert row is not None
    return row


async def _apply_delta(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    delta_wei: Decimal,
    reason: str,
    related_payment_id: uuid.UUID | None = None,
    related_topup_id: uuid.UUID | None = None,
    created_by_operator_id: uuid.UUID | None = None,
) -> CreditBalance:
    """Apply a signed delta to the user's balance and write a ledger row.

    The caller's transaction must be open. Raises `InsufficientCredit`
    when the resulting balance would be negative.
    """
    balance = await _ensure_balance_row(session, user_id=user_id)
    new_amount = balance.amount_wei + delta_wei
    if new_amount < 0:
        raise InsufficientCredit(
            available_wei=int(balance.amount_wei),
            required_wei=int(-delta_wei),
        )

    balance.amount_wei = new_amount
    balance.version = balance.version + 1
    session.add(
        CreditLedger(
            user_id=user_id,
            delta_wei=delta_wei,
            reason=reason,
            related_payment_id=related_payment_id,
            related_topup_id=related_topup_id,
            created_by_operator_id=created_by_operator_id,
        )
    )
    return balance


async def grant_initial_credit(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: int,
    operator_id: uuid.UUID,
) -> CreditTopup | None:
    """Grant the configured initial credit on operator approval.

    No-op when `amount_wei` is 0 (default in `.env.example`).
    """
    if amount_wei <= 0:
        return None

    topup = CreditTopup(
        user_id=user_id,
        amount_wei=Decimal(amount_wei),
        topup_kind="initial",
        operator_id=operator_id,
    )
    session.add(topup)
    await session.flush()
    await _apply_delta(
        session,
        user_id=user_id,
        delta_wei=Decimal(amount_wei),
        reason="topup",
        related_topup_id=topup.id,
        created_by_operator_id=operator_id,
    )
    return topup


async def topup(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: int,
    kind: str,
    operator_id: uuid.UUID | None,
) -> tuple[CreditTopup, CreditBalance]:
    """Generic topup. Records a CreditTopup row and the matching ledger entry."""
    topup_row = CreditTopup(
        user_id=user_id,
        amount_wei=Decimal(amount_wei),
        topup_kind=kind,
        operator_id=operator_id,
    )
    session.add(topup_row)
    await session.flush()
    balance = await _apply_delta(
        session,
        user_id=user_id,
        delta_wei=Decimal(amount_wei),
        reason="topup" if kind != "auto_replenish" else "auto_replenish",
        related_topup_id=topup_row.id,
        created_by_operator_id=operator_id,
    )
    return topup_row, balance


async def charge_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: Decimal,
    payment_id: uuid.UUID,
) -> CreditBalance:
    """Decrement balance by `amount_wei`, marking the change against a payment."""
    return await _apply_delta(
        session,
        user_id=user_id,
        delta_wei=-amount_wei,
        reason="payment_charge",
        related_payment_id=payment_id,
    )


async def refund_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: Decimal,
    payment_id: uuid.UUID,
) -> CreditBalance:
    """Refund `amount_wei` to the user against a payment (reconciliation)."""
    return await _apply_delta(
        session,
        user_id=user_id,
        delta_wei=amount_wei,
        reason="payment_refund",
        related_payment_id=payment_id,
    )


async def get_balance(
    session: AsyncSession, *, user_id: uuid.UUID
) -> CreditBalance:
    """Return the user's current balance row (creating it lazily if absent)."""
    row = await session.scalar(
        select(CreditBalance).where(CreditBalance.user_id == user_id)
    )
    if row is None:
        # Lazy-create with zero balance so callers always have a row to read.
        row = CreditBalance(user_id=user_id, amount_wei=Decimal(0), version=0)
        session.add(row)
        await session.flush()
    return row


async def list_ledger(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50
) -> list[CreditLedger]:
    """Most-recent-first slice of the user's ledger."""
    rows = await session.scalars(
        select(CreditLedger)
        .where(CreditLedger.user_id == user_id)
        .order_by(CreditLedger.created_at.desc())
        .limit(limit)
    )
    return list(rows)
