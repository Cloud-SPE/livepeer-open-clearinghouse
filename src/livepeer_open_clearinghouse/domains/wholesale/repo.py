"""Persistence for LOC's customer-independent wholesale accounting view."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class WholesaleAccount(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """Latest observed state for one stable Modules wholesale account.

    This table deliberately has no customer or API-key foreign key. Its identity
    is the protocol-owned ``(chain, payer, payee, denomination)`` tuple.
    """

    __table_args__ = (
        UniqueConstraint(
            "chain_id",
            "payer_eth_address",
            "payee_eth_address",
            "denomination",
            name="uq_wholesale_account_identity",
        ),
    )

    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payer_eth_address: Mapped[str] = mapped_column(String(42), nullable=False)
    payee_eth_address: Mapped[str] = mapped_column(String(42), nullable=False)
    denomination: Mapped[str] = mapped_column(String(16), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_url: Mapped[str] = mapped_column(nullable=False)
    credited_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    reserved_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    debited_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    available_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    remote_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(nullable=False)


class WholesaleExposureBudget(Base, TimestampMixin, TableNameFromClassMixin):
    """Singleton lock and conservative total for cross-payee funding claims."""

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    projected_available_wei: Mapped[Decimal] = mapped_column(nullable=False)


class WholesaleFunding(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One idempotent account-funding attempt and its broker acknowledgement."""

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wholesale_account.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    mint_request_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Opaque only: useful for recovery, but not an ownership or billing edge.
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_available_wei: Mapped[Decimal] = mapped_column(nullable=False)
    observed_available_wei: Mapped[Decimal] = mapped_column(nullable=False)
    requested_shortfall_wei: Mapped[Decimal] = mapped_column(nullable=False)
    route_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    payment_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    minted_expected_value_wei: Mapped[Decimal] = mapped_column(nullable=False)
    credited_value_wei: Mapped[Decimal | None] = mapped_column(nullable=True)
    work_id: Mapped[str | None] = mapped_column(nullable=True)
    account_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
