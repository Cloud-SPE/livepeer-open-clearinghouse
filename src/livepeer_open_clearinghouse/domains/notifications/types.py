"""Pydantic models for the notifications domain.

Resend's webhook payload shape (and the Standard Webhooks signing
protocol) is documented at https://resend.com/docs/dashboard/webhooks
— we model the load-bearing fields only and accept arbitrary extra
keys via `model_config = ConfigDict(extra="allow")` so future fields
don't reject valid payloads.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ResendEventData(BaseModel):
    """The `data` object on a Resend webhook payload."""

    model_config = ConfigDict(extra="allow")

    email_id: str | None = Field(default=None)
    to: list[str] = Field(default_factory=list)
    from_: str | None = Field(default=None, alias="from")
    subject: str | None = Field(default=None)


class ResendWebhookEvent(BaseModel):
    """A Resend webhook payload at the top level.

    The `type` field carries the event class — ``email.sent``,
    ``email.delivered``, ``email.bounced``, ``email.complained``,
    ``email.failed``, ``email.opened``, ``email.clicked``,
    ``email.delivery_delayed`` — we treat the string as opaque
    everywhere except the ``EmailSend.status`` mutation logic.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    created_at: datetime | None = None
    data: ResendEventData = Field(default_factory=ResendEventData)


class EmailEventView(BaseModel):
    """Outbound: one row from `GET /v1/admin/email/events`."""

    id: uuid.UUID
    provider_event_id: str
    email_send_id: uuid.UUID | None
    provider_message_id: str | None
    event_type: str
    to_address: str | None
    received_at: datetime


class EmailEventList(BaseModel):
    items: list[EmailEventView]


class WebhookAcceptedResponse(BaseModel):
    """The handler always returns 200 on accepted (or duplicate) events.

    Standard Webhooks senders interpret any non-2xx as a delivery
    failure and retry; we want explicit duplicate suppression to be
    transparent at the HTTP layer.
    """

    ok: bool = True
    duplicate: bool = False
    received_event_id: str | None = None
