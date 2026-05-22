"""Business logic for billing.

The ledger is the source of truth; ``credit_balance.amount_wei`` is a
denormalized mirror used for fast reads and ``SELECT ... FOR UPDATE``
locking. Every balance change goes through ``_apply_delta`` which
inserts a ledger row and increments the balance's version.

See docs/RELIABILITY.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.billing.repo import (
    CreditBalance,
    CreditLedger,
    CreditTopup,
    SpendWindow,
    UserBillingConfig,
)
from pymthouse.errors import InsufficientCredit, SpendCapExceeded
from pymthouse.providers.clock import Clock
from pymthouse.settings import Settings


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


def window_bounds_for(
    now: datetime, period_seconds: int
) -> tuple[datetime, datetime]:
    """Return the `[start, end)` of the spend-window containing `now`."""
    if now.tzinfo is None:
        raise ValueError("clock.now() must be tz-aware")
    epoch = int(now.timestamp())
    bucket = (epoch // period_seconds) * period_seconds
    start = datetime.fromtimestamp(bucket, tz=now.tzinfo)
    end = start + timedelta(seconds=period_seconds)
    return start, end


async def _get_or_create_spend_window(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
    cap_wei: int,
) -> SpendWindow:
    """Get-or-create the per-user spend_window row for this window, locked."""
    row = await session.scalar(
        select(SpendWindow)
        .where(
            SpendWindow.user_id == user_id,
            SpendWindow.window_start == window_start,
        )
        .with_for_update()
    )
    if row is None:
        row = SpendWindow(
            user_id=user_id,
            window_start=window_start,
            window_end=window_end,
            spent_wei=Decimal(0),
            cap_wei=Decimal(cap_wei),
        )
        session.add(row)
        await session.flush()
    return row


async def enforce_and_record_spend(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: Decimal,
    clock: Clock,
    period_seconds: int,
    cap_wei: int,
) -> None:
    """Reserve `amount_wei` against the current spend-window.

    A `cap_wei` of 0 means "no cap" — no row is touched. Otherwise the
    row is updated in place and `SpendCapExceeded` is raised if the cap
    would be breached.
    """
    if cap_wei <= 0:
        return
    now = clock.now()
    start, end = window_bounds_for(now, period_seconds)
    window = await _get_or_create_spend_window(
        session,
        user_id=user_id,
        window_start=start,
        window_end=end,
        cap_wei=cap_wei,
    )
    after = window.spent_wei + amount_wei
    if after > window.cap_wei:
        raise SpendCapExceeded(
            cap_wei=int(window.cap_wei),
            would_be_spent_wei=int(after),
        )
    window.spent_wei = after


async def remaining_window_room(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    clock: Clock,
    period_seconds: int,
    cap_wei: int,
) -> Decimal:
    """How much more the user can be charged in the current window.

    Returns ``Decimal('inf')`` when `cap_wei <= 0` (no cap configured).
    """
    if cap_wei <= 0:
        return Decimal("Infinity")
    now = clock.now()
    start, _ = window_bounds_for(now, period_seconds)
    row = await session.scalar(
        select(SpendWindow).where(
            SpendWindow.user_id == user_id, SpendWindow.window_start == start
        )
    )
    spent = Decimal(0) if row is None else row.spent_wei
    return Decimal(cap_wei) - spent


async def charge_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount_wei: Decimal,
    payment_id: uuid.UUID,
    clock: Clock,
    period_seconds: int,
    cap_wei: int,
) -> CreditBalance:
    """Decrement balance by `amount_wei`, recording the spend-window.

    Raises `SpendCapExceeded` (before any DB mutation) if the window
    cap would be breached; `InsufficientCredit` if balance is too low.
    """
    await enforce_and_record_spend(
        session,
        user_id=user_id,
        amount_wei=amount_wei,
        clock=clock,
        period_seconds=period_seconds,
        cap_wei=cap_wei,
    )
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


# ---------------------------------------------------------------------------
# Per-user billing config (overrides global Settings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedBillingConfig:
    """The effective billing knobs for one user — per-user override or global."""

    spend_period_seconds: int
    spend_period_cap_wei: int
    auto_replenish_increment_wei: int
    auto_replenish_threshold_wei: int


async def get_billing_config(
    session: AsyncSession, *, user_id: uuid.UUID
) -> UserBillingConfig | None:
    """Return the user's UserBillingConfig row if one exists."""
    return await session.scalar(
        select(UserBillingConfig).where(UserBillingConfig.user_id == user_id)
    )


async def resolve_billing_config(
    session: AsyncSession, *, user_id: uuid.UUID, settings: Settings
) -> ResolvedBillingConfig:
    """Per-user override with global fallback. Pure read — no writes."""
    row = await get_billing_config(session, user_id=user_id)
    if row is None:
        return ResolvedBillingConfig(
            spend_period_seconds=settings.default_spend_period_seconds,
            spend_period_cap_wei=settings.default_spend_period_cap_wei,
            auto_replenish_increment_wei=settings.auto_replenish_increment_wei,
            auto_replenish_threshold_wei=0,
        )
    return ResolvedBillingConfig(
        spend_period_seconds=(
            row.spend_period_seconds
            if row.spend_period_seconds is not None
            else settings.default_spend_period_seconds
        ),
        spend_period_cap_wei=(
            int(row.spend_period_cap_wei)
            if row.spend_period_cap_wei is not None
            else settings.default_spend_period_cap_wei
        ),
        auto_replenish_increment_wei=(
            int(row.auto_replenish_increment_wei)
            if row.auto_replenish_increment_wei is not None
            else settings.auto_replenish_increment_wei
        ),
        auto_replenish_threshold_wei=(
            int(row.auto_replenish_threshold_wei)
            if row.auto_replenish_threshold_wei is not None
            else 0
        ),
    )


async def upsert_billing_config(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operator_id: uuid.UUID,
    spend_period_seconds: int | None,
    spend_period_cap_wei: int | None,
    auto_replenish_increment_wei: int | None,
    auto_replenish_threshold_wei: int | None,
) -> UserBillingConfig:
    """Create or update a UserBillingConfig row. NULL values clear an override."""
    row = await get_billing_config(session, user_id=user_id)
    if row is None:
        row = UserBillingConfig(user_id=user_id)
        session.add(row)
    row.spend_period_seconds = spend_period_seconds
    row.spend_period_cap_wei = (
        Decimal(spend_period_cap_wei) if spend_period_cap_wei is not None else None
    )
    row.auto_replenish_increment_wei = (
        Decimal(auto_replenish_increment_wei)
        if auto_replenish_increment_wei is not None
        else None
    )
    row.auto_replenish_threshold_wei = (
        Decimal(auto_replenish_threshold_wei)
        if auto_replenish_threshold_wei is not None
        else None
    )
    row.updated_by_operator_id = operator_id
    await session.flush()
    return row
