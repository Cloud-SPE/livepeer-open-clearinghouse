"""Strict policy types for customer-independent wholesale funding."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WholesaleFundingLimits(BaseModel):
    """Operator exposure limits applied before any ticket is signed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_available_wei: Decimal = Field(gt=0)
    replenish_below_wei: Decimal = Field(gt=0)
    max_available_per_payee_wei: Decimal = Field(gt=0)
    max_aggregate_available_wei: Decimal = Field(gt=0)
    max_single_funding_wei: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def target_fits_hard_limits(self) -> WholesaleFundingLimits:
        if self.replenish_below_wei > self.target_available_wei:
            raise ValueError("replenish_below_wei exceeds target_available_wei")
        if self.target_available_wei > self.max_available_per_payee_wei:
            raise ValueError("target_available_wei exceeds the per-payee limit")
        if self.target_available_wei > self.max_aggregate_available_wei:
            raise ValueError("target_available_wei exceeds the aggregate limit")
        return self


class WholesaleFundingPlan(BaseModel):
    """Bounded result of applying operator policy to a trusted observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_available_wei: Decimal = Field(gt=0)
    observed_available_wei: Decimal = Field(ge=0)
    shortfall_wei: Decimal = Field(ge=0)
    projected_payee_available_wei: Decimal = Field(ge=0)
    projected_aggregate_available_wei: Decimal = Field(ge=0)
