"""Pydantic models for the admin domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PendingUserView(BaseModel):
    """A user not yet approved."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    email_verified_at: datetime | None
    created_at: datetime


class PendingUserList(BaseModel):
    items: list[PendingUserView]


class ApprovedUserView(BaseModel):
    """A user with an active approval."""

    user_id: uuid.UUID
    approved_at: datetime
    operator_id: uuid.UUID


class AdminUserView(BaseModel):
    """Admin listing row: identity + approval + balance, no secrets."""

    id: uuid.UUID
    email: str
    email_verified_at: datetime | None
    approved: bool
    balance_wei: int
    created_at: datetime


class AdminUserList(BaseModel):
    items: list[AdminUserView]
    total: int


class BillingConfigView(BaseModel):
    """Per-user billing config (null = inherit default)."""

    user_id: uuid.UUID
    spend_period_seconds: int | None
    spend_period_cap_wei: int | None
    auto_replenish_increment_wei: int | None
    auto_replenish_threshold_wei: int | None


class BillingConfigUpdate(BaseModel):
    """Inbound: ``PUT /v1/admin/users/{id}/billing-config``.

    Send `null` to clear an override and inherit the default; send an
    integer to set/replace it.
    """

    spend_period_seconds: int | None = None
    spend_period_cap_wei: int | None = None
    auto_replenish_increment_wei: int | None = None
    auto_replenish_threshold_wei: int | None = None


class EffectiveBillingConfigView(BaseModel):
    """The values that would be applied right now (overrides + defaults)."""

    spend_period_seconds: int
    spend_period_cap_wei: int
    auto_replenish_increment_wei: int
    auto_replenish_threshold_wei: int


class BillingConfigResponse(BaseModel):
    """Outbound: per-user config plus the effective values."""

    config: BillingConfigView
    effective: EffectiveBillingConfigView


class DepositSnapshotView(BaseModel):
    """One row from the periodic payment-daemon deposit poll."""

    id: uuid.UUID
    taken_at: datetime
    deposit_wei: int
    reserve_wei: int
    withdraw_round: int


class DepositSnapshotList(BaseModel):
    items: list[DepositSnapshotView]


class AuditEntryView(BaseModel):
    """One row of operator_audit, joined with the operator + target emails."""

    id: uuid.UUID
    operator_email: str
    action: str
    target_user_email: str | None
    target_user_id: uuid.UUID | None
    params: dict | None
    created_at: datetime


class AuditEntryList(BaseModel):
    items: list[AuditEntryView]


# ---- Operator management ---------------------------------------------------


class OperatorView(BaseModel):
    """Public-facing operator shape (no token material)."""

    id: uuid.UUID
    email: str
    name: str
    role: str
    last_login_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class OperatorList(BaseModel):
    items: list[OperatorView]


class CreateOperatorRequest(BaseModel):
    """Inbound: ``POST /v1/admin/operators``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="member")


class UpdateOperatorRequest(BaseModel):
    """Inbound: ``PATCH /v1/admin/operators/{id}`` — at least one field."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = None


class OperatorWithToken(BaseModel):
    """Outbound from create + rotate-token. The ``raw_token`` field is
    shown exactly once; the gateway only stores its hash."""

    operator: OperatorView
    raw_token: str


# ---- SDK approval list -----------------------------------------------------


class SdkApprovalView(BaseModel):
    """One row of sdk_approval — operator view."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lang: str
    version: str
    git_sha7: str
    status: str
    notes: str | None
    added_by_operator_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SdkApprovalList(BaseModel):
    items: list[SdkApprovalView]


class CreateSdkApprovalRequest(BaseModel):
    """Inbound: ``POST /v1/admin/sdk-approvals``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    lang: str = Field(min_length=1, max_length=32)
    version: str = Field(min_length=1, max_length=64)
    git_sha7: str = Field(min_length=4, max_length=64)
    status: str = "approved"
    notes: str | None = Field(default=None, max_length=500)


class UpdateSdkApprovalRequest(BaseModel):
    """Inbound: ``PATCH /v1/admin/sdk-approvals/{id}`` — at least one field."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: str | None = None
    notes: str | None = Field(default=None, max_length=500)


class SdkManifestEntry(BaseModel):
    """One row of the public SDK manifest."""

    lang: str
    version: str
    git_sha7: str
    status: str


class SdkManifest(BaseModel):
    """Public payload at ``GET /v1/sdk/manifest``."""

    items: list[SdkManifestEntry]
    generated_at: datetime


class SessionWithSdkView(BaseModel):
    """One row of the admin session-recent feed with the bucketed SDK
    approval status attached."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    api_key_id: uuid.UUID
    work_id: str
    capability: str
    offering: str
    mode: str
    state: str
    sdk_identity: str | None
    sdk_status: str
    opened_at: datetime
    closed_at: datetime | None


class SessionWithSdkList(BaseModel):
    items: list[SessionWithSdkView]


class SdkDistributionEntry(BaseModel):
    sdk_identity: str
    count: int
    status: str


class SdkDistributionResponse(BaseModel):
    items: list[SdkDistributionEntry]
