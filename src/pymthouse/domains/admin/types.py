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
