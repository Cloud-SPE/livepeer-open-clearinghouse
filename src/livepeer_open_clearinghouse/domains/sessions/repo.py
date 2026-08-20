"""ORM models for the sessions domain.

Tables:
    payment_session     — one row per long-lived session
    payment_settlement  — append-only event log keyed on session

The `state` column on PaymentSession is the lifecycle string:

  - ``open``      — session is live; broker may emit balance-low and
                    the SDK may request refills (extensible modes).
  - ``draining``  — the SDK or operator initiated close; we're
                    waiting on the broker's final reconcile.
  - ``closed``    — final settlement written; encumbered value
                    released; row is read-only.

`protocol` is the authoritative Modules protocol tag. `route_snapshot` is the
immutable signed-route projection used for billing and lifecycle decisions.

`outcome` follows the upstream `SettlementRecord.SettlementOutcome`
enum (``EXACT``, ``UNDERFUNDED``, ``OVERFUNDED``,
``STOPPED_AT_BUDGET``, ``TOPPED_UP``) and is nullable until close.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class PaymentSession(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """A long-running interaction opened via ``POST /v1/sessions``.

    The session encumbers ``funded_value_wei`` (= ``max_total_units``
    times the offering's EV-per-unit) from the user's balance at
    mint, guaranteeing per-session refill is bounded by construction.
    Encumbered value is released back to the balance at close as
    ``funded_value_wei - billed_value_wei``.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_key.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    work_id: Mapped[str] = mapped_column(nullable=False, index=True)
    capability: Mapped[str] = mapped_column(nullable=False)
    offering: Mapped[str] = mapped_column(nullable=False)
    protocol: Mapped[str] = mapped_column(nullable=False)
    route_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    broker_request_id: Mapped[str | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(nullable=False, index=True)
    estimated_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_total_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    funded_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    billed_value_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    actual_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    outcome: Mapped[str | None] = mapped_column(nullable=True)
    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sdk_identity: Mapped[str | None] = mapped_column(nullable=True)
    opened_at: Mapped[datetime] = mapped_column(nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_debit_seq: Mapped[int] = mapped_column(nullable=False, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(nullable=True)


class PaymentSettlement(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One settlement event for a session.

    ``event_type`` is one of:

      - ``refill_granted``    — LOC minted a top-up
      - ``refill_denied``     — cap blocked a refill
      - ``balance_low``       — broker emitted Livepeer-Balance-Low
      - ``close``             — session ended; final reconcile
      - ``reconcile``         — janitor finalized a silent session

    Append-only by convention; rows are never updated.
    """

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(nullable=False)
    actual_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billed_value_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    outcome: Mapped[str | None] = mapped_column(nullable=True)
    raw_record: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
