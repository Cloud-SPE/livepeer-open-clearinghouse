"""Pydantic models for the admin domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
