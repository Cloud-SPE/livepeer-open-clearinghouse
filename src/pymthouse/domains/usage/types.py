"""Pydantic models for the usage domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ReportUsageRequest(BaseModel):
    """Inbound: ``POST /v1/usage/report``.

    App developer self-reports the actuals for a previously-minted
    payment. The delta is refunded to their balance via the ledger.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    work_id: str = Field(min_length=1)
    actual_work_units: int = Field(ge=0)
    request_id: str | None = Field(default=None, max_length=200)


class UsageRecordView(BaseModel):
    """The persisted, first-write-wins reconciliation row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    payment_id: uuid.UUID
    actual_work_units: int
    actual_cost_wei: Decimal
    request_id: str | None
    created_at: datetime


class UsageReportResponse(BaseModel):
    """Outbound: ``POST /v1/usage/report``."""

    usage: UsageRecordView
    refunded_wei: Decimal
    payment_status: str
    new_balance_wei: Decimal
