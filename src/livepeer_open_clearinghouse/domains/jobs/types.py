"""Pydantic request/response types for the jobs domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from livepeer_open_clearinghouse.domains.sessions.types import CapStatus
from livepeer_open_clearinghouse.providers.registry_daemon import RouteBinding, RouteSnapshot


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
    transport: Literal["unary", "stream", "multipart"]
    estimated_units: int = Field(gt=0)
    max_total_units: int | None = Field(default=None, gt=0)
    route_binding: RouteBinding | None = None


class CreateJobResponse(BaseModel):
    """Outbound: ``POST /v1/jobs``.

    Carries the broker target + minted envelope so the SDK can issue
    its one-shot call to the broker directly (handoff mode). The
    ``settle_endpoint`` is the LOC URL the SDK posts to after reading
    the broker's response (terminal headers for unary/multipart, or a
    terminal settlement lookup when stream trailers are inaccessible).
    """

    job_id: uuid.UUID
    request_id: str
    work_id: str
    broker_url: str
    protocol: str
    transport: Literal["unary", "stream", "multipart"]
    work_unit: str
    route_snapshot: RouteSnapshot
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    settle_endpoint: str
    opened_at: datetime


class SettlementSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["secp256k1"]
    canonicalization: Literal["jcs"]
    value: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")


class SettlementEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    signature: SettlementSignature


class SettleJobRequest(BaseModel):
    """Inbound: ``POST /v1/jobs/{id}/settle``.

    SDK reports the broker's terminal claim and the required signed
    ``SettlementRecord`` from ``Livepeer-Settlement``. ``outcome`` is
    only an optional consistency assertion; signed settlement is
    authoritative for accounting.
    """

    actual_units: int = Field(ge=0)
    broker_job_id: str = Field(min_length=1)
    work_unit: str = Field(min_length=1)
    outcome: str | None = None
    settlement: SettlementEnvelope


class SettleJobResponse(BaseModel):
    """Outbound: ``POST /v1/jobs/{id}/settle``.

    Final accounting for the job: what the customer was billed and
    how much encumbered value is being refunded, plus a fresh
    ``cap_status`` snapshot. The cap_status here is informational —
    jobs are one-shot so ``will_refuse_next_refill`` and
    ``winddown_reason`` reflect whether the *next* mint of this
    size would be refused (e.g., spend-period cap is nearly full
    after this settlement). SDKs use it to surface "you're at N%
    of your monthly cap" UX after each completed job.
    """

    job_id: uuid.UUID
    work_id: str
    actual_units: int
    billed_value_wei: int
    refund_wei: int
    outcome: str
    closed_at: datetime
    cap_status: CapStatus


class JobStatusResponse(BaseModel):
    """Customer-visible job state without conflating billing evidence."""

    job_id: uuid.UUID
    request_id: str
    work_id: str
    state: str
    accounting_outcome: Literal[
        "unresolved",
        "non_admission_audit",
        "broker_settled",
        "conservative_full_charge",
    ]
    broker_exchange_outcome: str | None
    actual_units: int | None
    billed_value_wei: int | None
    funded_value_wei: int
    creation_round: int | None
    expires_after_round: int | None
    mint_ticket_validity_period: int | None
    mint_ticket_validity_period_observed_at: datetime | None
    observed_current_round: int | None
    current_ticket_validity_period: int | None
    current_ticket_validity_period_observed_at: datetime | None
    opened_at: datetime
    closed_at: datetime | None
