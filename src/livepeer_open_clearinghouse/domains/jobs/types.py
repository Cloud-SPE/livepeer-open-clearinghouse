"""Pydantic request/response types for the jobs domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateJobRequest(BaseModel):
    """Inbound: ``POST /v1/jobs``.

    SDK declares its intent for an atomic / post-settled / streaming
    job. ``max_total_units`` is the worst-case ceiling LOC encumbers
    up front; if omitted, defaults to ``estimated_units`` (the SDK is
    asserting "I know exactly what I need" — typical for case (a)).

    For case (b)/(c) workloads where output_tokens are unknown,
    customers should pass a generous ``max_total_units`` to give the
    broker room. Refunds happen at ``/settle``.
    """

    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    estimated_units: int = Field(gt=0)
    max_total_units: int | None = Field(default=None, gt=0)


class CreateJobResponse(BaseModel):
    """Outbound: ``POST /v1/jobs``.

    Carries the broker target + minted envelope so the SDK can issue
    its one-shot call to the broker directly (handoff mode). The
    ``settle_endpoint`` is the LOC URL the SDK posts to after reading
    the broker's response (``Livepeer-Work-Units`` header for
    http-reqresp / http-multipart, HTTP trailer for http-stream).
    """

    job_id: uuid.UUID
    work_id: str
    broker_url: str
    mode: str
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    settle_endpoint: str
    opened_at: datetime


class SettleJobRequest(BaseModel):
    """Inbound: ``POST /v1/jobs/{id}/settle``.

    SDK reports the final actual_units read from the broker's
    ``Livepeer-Work-Units`` header/trailer. Optional outcome +
    settlement (the parsed ``SettlementRecord`` if the broker emitted
    one in the ``Livepeer-Settlement`` header).
    """

    actual_units: int = Field(ge=0)
    outcome: str | None = None
    settlement: dict[str, Any] | None = None


class SettleJobResponse(BaseModel):
    """Outbound: ``POST /v1/jobs/{id}/settle``.

    Final accounting for the job: what the customer was billed and
    how much encumbered value is being refunded.
    """

    job_id: uuid.UUID
    work_id: str
    actual_units: int
    billed_value_wei: int
    refund_wei: int
    outcome: str
    closed_at: datetime
