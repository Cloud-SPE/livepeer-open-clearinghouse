"""Pydantic request/response types for the sessions domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    """Inbound: ``POST /v1/sessions``.

    The SDK declares its intent for a long-lived session: the
    capability + offering pair to bill against, the estimated runway
    (best-guess of what the session is likely to consume), and the
    absolute ceiling (``max_total_units``) that LOC will encumber up
    front under handoff-mode's worst-case sizing rule.

    ``max_total_units`` MUST be >= ``estimated_runway_units`` and > 0.
    """

    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    estimated_runway_units: int = Field(gt=0)
    max_total_units: int = Field(gt=0)


class CreateSessionResponse(BaseModel):
    """Outbound: ``POST /v1/sessions``.

    Carries everything the SDK needs to open the broker-side session
    and bookkeep the LOC-side lifecycle. The ``payment_envelope``
    is base64-encoded wire-format Payment bytes — the SDK attaches
    it as the ``Livepeer-Payment`` HTTP header (or upgrade header,
    for WS modes) when connecting to ``broker_url``.

    Per exec-plan 002 handoff design, LOC never sits in the data
    path: ``broker_url`` is the orchestrator's HTTP/WS endpoint the
    SDK talks to directly. The two ``*_endpoint`` fields are
    LOC-relative paths the SDK calls when it needs to refill or
    explicitly close the session.
    """

    session_id: uuid.UUID
    work_id: str
    broker_url: str
    mode: str
    payment_envelope: str
    expected_value_wei: int
    funded_value_wei: int
    refill_endpoint: str
    close_endpoint: str
    opened_at: datetime
