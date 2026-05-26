"""ORM models for the notifications domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from livepeer_open_clearinghouse.providers.db.base import (
    Base,
    TableNameFromClassMixin,
    TimestampMixin,
    UuidPkMixin,
)


class EmailSend(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One row per accepted outbound email.

    `provider_message_id` is the upstream ID (Resend's ``id`` on a
    successful send). It's the correlation key webhook events arrive
    against — webhook payloads carry the same ID under
    ``data.email_id``.

    `status` is purely a server-side summary derived from later events;
    it starts as ``"sent"`` and can transition to ``"delivered"``,
    ``"bounced"``, ``"complained"``, ``"failed"`` as events come in.

    Note: this table isn't populated by the current outbound-email
    path. Wiring it up means the providers/email/ provider would have
    to reach into a domain repo (forbidden by the layered-arch lint),
    so the right place is in each calling service after a successful
    send. Tracked as a follow-up in tech-debt; the table is created
    now so we don't need another migration when the wiring lands.
    """

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    to_address: Mapped[str] = mapped_column(nullable=False, index=True)
    subject: Mapped[str] = mapped_column(nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    status: Mapped[str] = mapped_column(nullable=False, default="sent")
    sent_at: Mapped[datetime] = mapped_column(nullable=False, index=True)


class EmailEvent(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One row per webhook callback we've accepted.

    `provider_event_id` is the Standard Webhooks ``webhook-id``; it's
    UNIQUE so retries (which Resend will absolutely do) are silently
    de-duplicated at insert time.

    `email_send_id` links back to the corresponding `EmailSend` row
    when we can correlate via `provider_message_id`. Webhook events for
    sends that pre-date this table (or that we missed recording) leave
    it NULL — `to_address` is kept on the event row so we can still
    aggregate by recipient.
    """

    provider_event_id: Mapped[str] = mapped_column(nullable=False, unique=True)
    email_send_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_send.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(nullable=False, index=True)
    to_address: Mapped[str | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(nullable=False, index=True)


class NotificationConfig(Base, TimestampMixin, TableNameFromClassMixin):
    """One operator-managed override of the default notification policy.

    Composite PK on (user_id, trigger, channel). A missing row means
    "use the operator default" — service code never materializes
    defaults eagerly. Customer disables a trigger by writing an
    enabled=False row; re-enables by deleting it (or writing
    enabled=True if the operator's default is False).
    """

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "trigger", "channel", name="pk_notification_config"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class PortalNotification(Base, UuidPkMixin, TimestampMixin, TableNameFromClassMixin):
    """One in-portal banner. ``dismissed_at`` flips when the customer
    closes the banner via POST /v1/notifications/{id}/dismiss."""

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(nullable=False)
    dismissed_at: Mapped[datetime | None] = mapped_column(nullable=True)


class NotificationWebhookConfig(Base, TimestampMixin, TableNameFromClassMixin):
    """Per-user webhook destination + last-test marker.

    Composite PK on user_id; one row per user. Signing secret is
    derived from a server-side seed at send time (see
    notifications.webhook), so no secret material lives on this row.
    """

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    last_test_at: Mapped[datetime | None] = mapped_column(nullable=True)
