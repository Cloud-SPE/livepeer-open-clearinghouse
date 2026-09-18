"""Strict policy types for customer-independent wholesale funding."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field, model_validator

SettlementDomainId = NewType("SettlementDomainId", str)
"""Opaque identity of one independently persisted settlement ledger."""

# Historical rows created before protocol major 4 remain in this namespace for
# audit/migration only. It is never valid on a new paid-work route.
COMPAT_SETTLEMENT_DOMAIN_ID = SettlementDomainId("loc.compat.wholesale-account/1.1.0-draft")
_SETTLEMENT_DOMAIN_ID = re.compile(r"^0x[0-9a-f]{64}$")


def settlement_domain_id(value: str) -> SettlementDomainId:
    """Parse the protocol-v4 nonzero, lowercase 256-bit ledger identity."""

    if _SETTLEMENT_DOMAIN_ID.fullmatch(value) is None or int(value[2:], 16) == 0:
        raise ValueError("settlement_domain_id must be a nonzero lowercase 0x-prefixed uint256")
    return SettlementDomainId(value)


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
