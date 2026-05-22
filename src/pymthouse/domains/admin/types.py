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
