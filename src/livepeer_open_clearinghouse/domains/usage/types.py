"""Wire models for the usage domain.

Usage is derived from settled ``payment_session`` rows — the one place
where capability, offering, key, units, funded and billed value all meet.
Every wei field goes out as an integer string (see ``providers.wire``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from livepeer_open_clearinghouse.providers.wire import WeiDecimal

AccountingOutcome = Literal["open", "unresolved", "broker_settled", "conservative_full_charge"]


class UsageJobView(BaseModel):
    """One paid job or session as the customer experiences it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    protocol: str
    capability: str
    offering: str
    api_key_id: uuid.UUID
    api_key_label: str | None
    sdk_identity: str | None
    state: str
    accounting_outcome: AccountingOutcome
    work_unit: str
    estimated_units: int
    max_total_units: int
    actual_units: int | None
    funded_value_wei: WeiDecimal
    billed_value_wei: WeiDecimal | None
    refunded_wei: WeiDecimal | None
    held_wei: WeiDecimal
    opened_at: datetime
    closed_at: datetime | None
    duration_seconds: float | None
    blocked_reason: str | None = None
    reported_units: int | None = None
    # Present on operator views only.
    user_id: uuid.UUID | None = None
    user_email: str | None = None


class UsageJobPage(BaseModel):
    items: list[UsageJobView]
    total: int
    limit: int
    offset: int


class UsageTotals(BaseModel):
    jobs: int
    open_jobs: int
    closed_jobs: int
    billed_wei: WeiDecimal
    refunded_wei: WeiDecimal
    held_wei: WeiDecimal


class OfferingUsage(BaseModel):
    capability: str
    offering: str
    protocol: str
    work_unit: str
    jobs: int
    units: int
    billed_wei: WeiDecimal
    held_wei: WeiDecimal


class ApiKeyUsage(BaseModel):
    api_key_id: uuid.UUID
    label: str | None
    jobs: int
    billed_wei: WeiDecimal


class DayUsage(BaseModel):
    day: str
    jobs: int
    billed_wei: WeiDecimal


class UserUsage(BaseModel):
    user_id: uuid.UUID
    email: str | None
    jobs: int
    billed_wei: WeiDecimal
    held_wei: WeiDecimal


class UsageSummary(BaseModel):
    since: datetime
    until: datetime
    totals: UsageTotals
    by_offering: list[OfferingUsage]
    by_api_key: list[ApiKeyUsage]
    by_day: list[DayUsage]
    # Fleet view only.
    by_user: list[UserUsage] | None = None


class UsagePeriod(BaseModel):
    start: datetime
    end: datetime
    seconds: int
    cap_wei: WeiDecimal | None
    pct_used: float | None


class UsageOverview(BaseModel):
    """The three figures a customer needs: available, held, spent."""

    available_wei: WeiDecimal
    held_wei: WeiDecimal
    spent_period_wei: WeiDecimal
    spent_30d_wei: WeiDecimal
    open_jobs: int
    period: UsagePeriod
    by_day: list[DayUsage]


class UnresolvedJob(BaseModel):
    job_id: uuid.UUID
    user_id: uuid.UUID
    user_email: str | None
    protocol: str
    capability: str
    offering: str
    funded_value_wei: WeiDecimal
    opened_at: datetime
    age_seconds: float
    blocked_reason: str | None = None
    reported_units: int | None = None


class SettlementFailure(BaseModel):
    at: datetime
    user_id: uuid.UUID | None
    user_email: str | None
    protocol: str | None
    session_id: str | None
    code: str
    reason: str | None


class AttentionCounts(BaseModel):
    unresolved: int
    settlement_failures_24h: int


class UsageAttention(BaseModel):
    counts: AttentionCounts
    unresolved: list[UnresolvedJob]
    settlement_failures: list[SettlementFailure]
