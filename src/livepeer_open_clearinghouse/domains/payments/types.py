"""Pydantic models for the payments domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MintPaymentRequest(BaseModel):
    """Inbound: ``POST /v1/payments/mint``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    work_units: int = Field(gt=0, le=10_000_000)


class MintPaymentResponse(BaseModel):
    """Outbound: ``POST /v1/payments/mint`` success."""

    payment_id: uuid.UUID
    work_id: str
    payment_bytes: str = Field(description="Base64-encoded; goes in Livepeer-Payment header")
    expected_value_wei: Decimal
    funded_value_wei: Decimal
    recipient_eth_address: str
    capability: str
    offering: str
    work_units_requested: int


class PaymentView(BaseModel):
    """Outbound: a single payment row in user-facing JSON."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    work_id: str
    recipient_eth_address: str
    capability: str
    offering: str
    work_units_requested: int
    funded_value_wei: Decimal
    expected_value_wei: Decimal
    reserved_wei: Decimal
    refunded_wei: Decimal
    status: str
    created_at: datetime
    updated_at: datetime


class PaymentList(BaseModel):
    items: list[PaymentView]
