"""Pydantic models for the telemetry domain.

The wire shape mirrors the universal-fields contract in
exec-plan 002 §"SDK telemetry (v1)" → "Universal fields". SDKs send
batches keyed on ``events``; each event carries the universal fields
at the top level plus an opaque ``payload`` object with the event-
type-specific shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IngestEventIn(BaseModel):
    """One inbound event in a batch."""

    model_config = ConfigDict(str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=64)
    event_schema_version: int = Field(ge=1)
    correlation_id: uuid.UUID | None = None
    client_ts: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    """Inbound: ``POST /v1/telemetry``. A batch of events from one SDK
    process."""

    events: list[IngestEventIn] = Field(min_length=1)


class IngestResponse(BaseModel):
    """Outbound: ``POST /v1/telemetry`` 202 response."""

    accepted: int
    rejected: int = 0
    # Per-event rejection reasons, ordered by input index. Empty when
    # ``rejected == 0``.
    rejections: list[str] = Field(default_factory=list)
