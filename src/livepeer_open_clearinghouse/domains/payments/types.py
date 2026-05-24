"""Pydantic models for the payments domain (read-only surface).

Post-exec-plan-002 cleanup, the mint request/response types moved
to ``domains/jobs/types.py`` (cases a/b/c) and
``domains/sessions/types.py`` (case d). What remains:

  - ``PaymentView`` — the customer-visible shape of a single
    ``payment`` row.
  - ``PaymentList`` — pagination wrapper around it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


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
