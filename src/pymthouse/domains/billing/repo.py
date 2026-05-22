"""ORM models and queries for the billing domain.

Tables:
    credit_balance      — denormalized current balance per user (FOR UPDATE target)
    credit_ledger       — append-only ledger; running sum is the source of truth
    credit_topup        — operator-initiated topups (initial, manual, auto_replenish)
    spend_window        — period-bounded spend tally per user

See docs/RELIABILITY.md for the row-locking + ledger semantics.

Wei amounts are `Mapped[Decimal]`; Base.type_annotation_map maps Decimal
to NUMERIC(78, 0).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pymthouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class CreditBalance(Base, TimestampMixin, TableNameFromClassMixin):
    """Per-user denormalized current balance.

    The authoritative balance is the running sum of `credit_ledger`. This
    table exists for fast reads and for `SELECT ... FOR UPDATE` locking.
    `version` is incremented on every write for cheap concurrency detection.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    amount_wei: Mapped[Decimal] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class CreditLedger(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """Append-only ledger of every balance change.

    `delta_wei` is signed: positive for credits, negative for debits. The
    `reason` is one of: 'topup', 'auto_replenish', 'payment_charge',
    'payment_refund', 'admin_adjustment'.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delta_wei: Mapped[Decimal] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(nullable=False)
    related_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment.id", ondelete="SET NULL"), nullable=True
    )
    related_topup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("credit_topup.id", ondelete="SET NULL"), nullable=True
    )
    created_by_operator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operator.id", ondelete="SET NULL"), nullable=True
    )


class CreditTopup(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """An operator-initiated topup or an auto-replenish event."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount_wei: Mapped[Decimal] = mapped_column(nullable=False)
    topup_kind: Mapped[str] = mapped_column(nullable=False)
    operator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operator.id", ondelete="SET NULL"), nullable=True
    )


class SpendWindow(Base, TimestampMixin, TableNameFromClassMixin):
    """Per-user spend tally for one period window.

    `cap_wei` is snapshotted at window start so changes to the per-user cap
    take effect on the next window, not retroactively.
    """

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "window_start", name="pk_spend_window"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    window_start: Mapped[datetime] = mapped_column(nullable=False)
    window_end: Mapped[datetime] = mapped_column(nullable=False)
    spent_wei: Mapped[Decimal] = mapped_column(nullable=False)
    cap_wei: Mapped[Decimal] = mapped_column(nullable=False)


class UserBillingConfig(Base, TimestampMixin, TableNameFromClassMixin):
    """Per-user override of billing knobs.

    Any NULL column falls back to the corresponding global Settings value.
    Created lazily by the admin endpoint; absent rows mean "all defaults."
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    spend_period_seconds: Mapped[int | None] = mapped_column(nullable=True)
    spend_period_cap_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    auto_replenish_increment_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    auto_replenish_threshold_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    updated_by_operator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operator.id", ondelete="SET NULL"), nullable=True
    )
