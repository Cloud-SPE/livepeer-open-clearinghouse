"""Pydantic models for the admin domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from livepeer_open_clearinghouse.providers.wire import WeiDecimal


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
    balance_wei: WeiDecimal
    created_at: datetime


class AdminUserList(BaseModel):
    items: list[AdminUserView]
    total: int


class BillingConfigView(BaseModel):
    """Per-user billing config (null = inherit default)."""

    user_id: uuid.UUID
    spend_period_seconds: int | None
    spend_period_cap_wei: WeiDecimal | None
    auto_replenish_increment_wei: WeiDecimal | None
    auto_replenish_threshold_wei: WeiDecimal | None


class BillingConfigUpdate(BaseModel):
    """Inbound: ``PUT /v1/admin/users/{id}/billing-config``.

    Send `null` to clear an override and inherit the default; send an
    integer to set/replace it.
    """

    spend_period_seconds: int | None = None
    spend_period_cap_wei: WeiDecimal | None = None
    auto_replenish_increment_wei: WeiDecimal | None = None
    auto_replenish_threshold_wei: WeiDecimal | None = None


class EffectiveBillingConfigView(BaseModel):
    """The values that would be applied right now (overrides + defaults)."""

    spend_period_seconds: int
    spend_period_cap_wei: WeiDecimal
    auto_replenish_increment_wei: WeiDecimal
    auto_replenish_threshold_wei: WeiDecimal


class BillingConfigResponse(BaseModel):
    """Outbound: per-user config plus the effective values."""

    config: BillingConfigView
    effective: EffectiveBillingConfigView


class DepositSnapshotView(BaseModel):
    """One row from the periodic payment-daemon deposit poll."""

    id: uuid.UUID
    taken_at: datetime
    deposit_wei: WeiDecimal
    reserve_wei: WeiDecimal
    withdraw_round: int
    current_round: int | None
    ticket_validity_period: int | None
    ticket_validity_period_observed_at: datetime | None


class DepositSnapshotList(BaseModel):
    items: list[DepositSnapshotView]


class AuditEntryView(BaseModel):
    """One row of operator_audit, joined with the operator + target emails."""

    id: uuid.UUID
    operator_email: str
    action: str
    target_user_email: str | None
    target_user_id: uuid.UUID | None
    params: dict[str, Any] | None
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
    """Public payload at ``GET /v1/sdk/manifest``.

    When the operator has configured a signing key, the response also
    carries ``signature`` (Ed25519 over the canonical JSON of
    ``{items, generated_at}``) and ``key_fingerprint`` (first 16 hex
    of SHA-256 of the public key). SDKs verify by fetching the
    public key at ``/v1/sdk/manifest/pubkey`` and recomputing.
    """

    items: list[SdkManifestEntry]
    generated_at: datetime
    signature: str | None = None
    key_fingerprint: str | None = None


class SdkManifestPubkey(BaseModel):
    """Public payload at ``GET /v1/sdk/manifest/pubkey``."""

    public_key: str  # base64 of the 32-byte raw Ed25519 public key
    key_fingerprint: str
    algorithm: str = "ed25519"


class SessionWithSdkView(BaseModel):
    """One row of the admin session-recent feed with the bucketed SDK
    approval status attached."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    api_key_id: uuid.UUID
    work_id: str
    capability: str
    offering: str
    protocol: str
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


ResolveAction = Literal["refund_hold", "accept_reported", "charge_full"]


class ResolveJobRequest(BaseModel):
    """Inbound: ``POST /v1/admin/jobs/{id}/resolve``.

    An operator's explicit decision for a job or session that cannot settle
    on its own. ``refund_hold`` releases the encumbrance, ``accept_reported``
    charges the broker-reported units at the snapshot price, ``charge_full``
    charges the funded value.
    """

    action: ResolveAction
    note: str | None = Field(default=None, max_length=500)


class ResolveJobResponse(BaseModel):
    job_id: uuid.UUID
    protocol: str
    action: ResolveAction
    state: str
    outcome: str
    actual_units: int | None
    funded_value_wei: WeiDecimal
    billed_value_wei: WeiDecimal
    refund_wei: WeiDecimal
    resolved_at: datetime


class WholesaleLimitsView(BaseModel):
    enabled: bool
    target_available_wei: WeiDecimal
    replenish_below_wei: WeiDecimal
    max_available_per_payee_wei: WeiDecimal
    max_aggregate_available_wei: WeiDecimal
    max_single_funding_wei: WeiDecimal


class WholesaleAccountView(BaseModel):
    id: uuid.UUID
    chain_id: int
    payer_eth_address: str
    payee_eth_address: str
    denomination: str
    protocol_version: str
    broker_url: str
    credited_value_wei: WeiDecimal
    reserved_value_wei: WeiDecimal
    debited_value_wei: WeiDecimal
    available_value_wei: WeiDecimal
    remote_version: int
    observed_at: datetime
    age_seconds: float
    stale: bool
    over_per_payee_limit: bool


class WholesaleFundingView(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    mint_request_id: str
    correlation_id: str | None
    target_available_wei: WeiDecimal
    observed_available_wei: WeiDecimal
    requested_shortfall_wei: WeiDecimal
    minted_expected_value_wei: WeiDecimal
    credited_value_wei: WeiDecimal | None
    work_id: str | None
    account_version: int | None
    status: str
    has_replayable_payment: bool
    acknowledged_at: datetime | None
    created_at: datetime
    age_seconds: float
    needs_attention: bool


class WholesaleOverview(BaseModel):
    generated_at: datetime
    limits: WholesaleLimitsView
    projected_available_wei: WeiDecimal
    observed_available_wei: WeiDecimal
    aggregate_headroom_wei: WeiDecimal
    aggregate_limit_exceeded: bool
    stale_accounts: int
    pending_fundings: int
    accounts: list[WholesaleAccountView]
    fundings: list[WholesaleFundingView]
