"""Pydantic models for the api_keys domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateApiKeyRequest(BaseModel):
    """Inbound: ``POST /v1/accounts/me/api-keys``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=64)


class ApiKeyView(BaseModel):
    """The non-secret view of a key."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prefix: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class CreateApiKeyResponse(BaseModel):
    """Outbound: includes the raw key one time only."""

    key: ApiKeyView
    raw_key: str = Field(description="Shown only at creation; PymtHouse cannot recover it later")


class ApiKeyList(BaseModel):
    """Outbound: list of a user's keys."""

    items: list[ApiKeyView]
