"""Pydantic models for the billing domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from livepeer_open_clearinghouse.providers.wire import WeiDecimal


class BalanceView(BaseModel):
    """The user-facing snapshot of credit state."""

    user_id: uuid.UUID
    amount_wei: WeiDecimal
    updated_at: datetime


class LedgerEntryView(BaseModel):
    """One row of the credit ledger (audit history)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    delta_wei: WeiDecimal
    reason: str
    related_payment_id: uuid.UUID | None
    related_engagement_id: uuid.UUID | None = None
    related_topup_id: uuid.UUID | None
    created_at: datetime


class LedgerPage(BaseModel):
    items: list[LedgerEntryView]


class TopupRequest(BaseModel):
    """Admin-side topup body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    amount_wei: WeiDecimal = Field(gt=0)
    kind: str = Field(default="manual", pattern=r"^(manual|initial|auto_replenish)$")


class TopupView(BaseModel):
    """Admin-side topup result."""

    topup_id: uuid.UUID
    new_balance_wei: WeiDecimal
    created_at: datetime


class CustomerPricingSnapshot(BaseModel):
    """Immutable LOC retail policy stored on one customer engagement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["customer-pricing/v1"] = "customer-pricing/v1"
    plan_id: str = Field(min_length=1)
    kind: Literal["wholesale_pass_through", "cost_plus", "unit_price"]
    denomination: Literal["wei"] = "wei"
    work_unit: str = Field(min_length=1)
    price_per_unit_wei: Decimal | None = Field(default=None, ge=0)
    units_per_price: int | None = Field(default=None, gt=0)
    fee_basis_points: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def fields_match_policy(self) -> CustomerPricingSnapshot:
        if self.price_per_unit_wei is not None and (
            self.price_per_unit_wei != self.price_per_unit_wei.to_integral_value()
        ):
            raise ValueError("retail price must be whole wei")
        has_unit_price = self.price_per_unit_wei is not None or self.units_per_price is not None
        if self.kind == "unit_price" and (
            self.price_per_unit_wei is None or self.units_per_price is None
        ):
            raise ValueError("unit_price policy requires price and units")
        if self.kind != "unit_price" and has_unit_price:
            raise ValueError("only unit_price policy carries retail unit pricing")
        if self.kind == "cost_plus" and self.fee_basis_points is None:
            raise ValueError("cost_plus policy requires fee_basis_points")
        if self.kind != "cost_plus" and self.fee_basis_points is not None:
            raise ValueError("only cost_plus policy carries fee_basis_points")
        return self
