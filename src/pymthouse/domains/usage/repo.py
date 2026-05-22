"""ORM models and queries for the usage domain.

Tables:
    usage_record — first-report-wins reconciliation of actual usage to a payment
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pymthouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class UsageRecord(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """An app-dev-reported actual usage record reconciled against one payment.

    Uniqueness on `(api_key_id, payment_id)` means the first report wins:
    duplicates return the original record without modifying state. See
    docs/RELIABILITY.md.
    """

    __table_args__ = (
        UniqueConstraint(
            "api_key_id", "payment_id", name="uq_usage_record_api_key_payment"
        ),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment.id", ondelete="RESTRICT"), nullable=False
    )
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("api_key.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    actual_work_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_cost_wei: Mapped[Decimal] = mapped_column(nullable=False)
    request_id: Mapped[str | None] = mapped_column(nullable=True)
