"""Pydantic models for the billing domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BalanceView(BaseModel):
    """The user-facing snapshot of credit state."""

    user_id: uuid.UUID
    amount_wei: Decimal
    updated_at: datetime


class LedgerEntryView(BaseModel):
    """One row of the credit ledger (audit history)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    delta_wei: Decimal
    reason: str
    related_payment_id: uuid.UUID | None
    related_topup_id: uuid.UUID | None
    created_at: datetime


class LedgerPage(BaseModel):
    items: list[LedgerEntryView]


class TopupRequest(BaseModel):
    """Admin-side topup body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    amount_wei: int = Field(gt=0)
    kind: str = Field(default="manual", pattern=r"^(manual|initial|auto_replenish)$")


class TopupView(BaseModel):
    """Admin-side topup result."""

    topup_id: uuid.UUID
    new_balance_wei: Decimal
    created_at: datetime
