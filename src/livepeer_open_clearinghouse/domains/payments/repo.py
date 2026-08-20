"""ORM models and queries for the payments domain.

Tables:
    payment                     — one row per ticket-mint call
    payment_idempotency_key     — idempotency-key ledger; in-flight + completed
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Integer, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class Payment(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A single payment-daemon `CreatePayment` call recorded on Livepeer Open Clearinghouse's side.

    `status` is the state machine from docs/RELIABILITY.md:
    'reserved' | 'issued' | 'reconciled' | 'refused'.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_key.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Nullable FK to the session this payment belongs to. NULL for the
    # legacy single-shot mint path; populated for both initial mints
    # and refills under exec-plan 002 handoff mode.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_session.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    work_id: Mapped[str] = mapped_column(nullable=False, index=True)
    recipient_eth_address: Mapped[str] = mapped_column(nullable=False)
    capability: Mapped[str] = mapped_column(nullable=False)
    offering: Mapped[str] = mapped_column(nullable=False)
    work_units_requested: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_per_work_unit_wei: Mapped[Decimal] = mapped_column(nullable=False)
    funded_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    expected_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    reserved_wei: Mapped[Decimal] = mapped_column(nullable=False)
    refunded_wei: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    status: Mapped[str] = mapped_column(nullable=False)
    refused_reason: Mapped[str | None] = mapped_column(nullable=True)


class PaymentIdempotencyKey(Base, TimestampMixin, TableNameFromClassMixin):
    """Durable create-request ledger keyed by account, operation, and key.

    The claim is committed before calling the payer daemon.  The completed
    outcome is committed atomically with the payment/session and balance
    mutation, so an identical HTTP retry can replay without minting again.
    """

    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id",
            "operation",
            "idempotency_key",
            name="pk_payment_idempotency_key",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    api_key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_key.id", ondelete="RESTRICT"))
    operation: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)


class PaymentDaemonDepositSnapshot(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """Periodic poll of payment-daemon.GetDepositInfo.

    The deposit/reserve numbers reflect the *pooled* wallet's view on
    the TicketBroker contract. Drawdown over time approximates the
    on-chain cost of all redeemed tickets across users. Used to detect
    operationally material variance from the EV-at-issuance charging
    model. See docs/RELIABILITY.md.
    """

    taken_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    deposit_wei: Mapped[Decimal] = mapped_column(nullable=False)
    reserve_wei: Mapped[Decimal] = mapped_column(nullable=False)
    withdraw_round: Mapped[int] = mapped_column(BigInteger, nullable=False)
