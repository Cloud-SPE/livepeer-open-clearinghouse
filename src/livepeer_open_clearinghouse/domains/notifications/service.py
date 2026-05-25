"""notifications.service — webhook ingestion + event log access.

Three concerns live here:

1. Verify the inbound webhook signature (Standard Webhooks HMAC-SHA256).
2. Persist the event idempotently (the ``provider_event_id`` unique
   constraint on `EmailEvent` lets us treat duplicate deliveries as
   no-ops).
3. Translate the event into a status mutation on the corresponding
   `EmailSend` row when we can correlate one (a bounce/complaint
   flagging the address is the load-bearing operator signal).

The signature verification follows
https://www.standardwebhooks.com/verifying — base64-encoded HMAC of
``{webhook-id}.{webhook-timestamp}.{body}`` compared against any of the
``v1,<sig>`` entries in the ``webhook-signature`` header (Resend rotates
keys by emitting multiple).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.notifications.config import (
    WEBHOOK_TIMESTAMP_TOLERANCE,
)
from livepeer_open_clearinghouse.domains.notifications.repo import (
    EmailEvent,
    EmailSend,
)
from livepeer_open_clearinghouse.domains.notifications.types import (
    ResendWebhookEvent,
)
from livepeer_open_clearinghouse.providers.clock import Clock


class WebhookSignatureError(Exception):
    """Raised when the inbound webhook fails signature verification.

    The handler converts this into a 401 — Standard Webhooks senders
    treat that as a permanent rejection (no retry), which is what we
    want for a bad signature.
    """


def verify_signature(
    *,
    secret: str,
    webhook_id: str | None,
    webhook_timestamp: str | None,
    webhook_signature: str | None,
    body: bytes,
    clock: Clock,
) -> None:
    """Raise ``WebhookSignatureError`` if any of the four checks fail.

    Checks (in order):
      1. All three headers + the secret are non-empty.
      2. The timestamp is within ``WEBHOOK_TIMESTAMP_TOLERANCE`` of now
         (replay protection).
      3. The HMAC-SHA256 of ``{id}.{ts}.{body}`` matches at least one
         signature in the ``v1,<base64>`` space-separated list.

    Constant-time comparison via ``hmac.compare_digest``.
    """
    if not (secret and webhook_id and webhook_timestamp and webhook_signature):
        raise WebhookSignatureError("missing signature headers")

    try:
        ts = int(webhook_timestamp)
    except ValueError as exc:
        raise WebhookSignatureError("non-integer webhook-timestamp") from exc

    now = int(clock.now().timestamp())
    if abs(now - ts) > int(WEBHOOK_TIMESTAMP_TOLERANCE.total_seconds()):
        raise WebhookSignatureError("webhook-timestamp out of tolerance")

    # The Standard Webhooks secret format is `whsec_<base64>`. Strip the
    # `whsec_` prefix (if present) and base64-decode to get the HMAC key.
    raw_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw_secret)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        # Fall back to treating the secret literally — some self-hosted
        # backends document a raw-string secret instead of the base64 form.
        key = secret.encode("utf-8")

    signed = f"{webhook_id}.{webhook_timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    presented = _extract_v1_signatures(webhook_signature)
    if not any(hmac.compare_digest(expected, p) for p in presented):
        raise WebhookSignatureError("signature mismatch")


def _extract_v1_signatures(header: str) -> Iterable[str]:
    """Pull `v1,<sig>` tokens out of the space-separated header value.

    Resend (and the Standard Webhooks spec) emit multiple signatures
    during key rotation; any one matching is sufficient.
    """
    for token in header.split():
        if "," in token:
            scheme, _, sig = token.partition(",")
            if scheme == "v1" and sig:
                yield sig


async def ingest_event(
    session: AsyncSession,
    *,
    event: ResendWebhookEvent,
    provider_event_id: str,
    clock: Clock,
) -> tuple[EmailEvent | None, bool]:
    """Persist a verified event; return (row, was_new).

    Idempotent by ``provider_event_id``: a re-delivery of the same
    event (Standard Webhooks retries on a non-2xx, even after we ack
    once) returns the existing row with ``was_new=False`` and no DB
    mutation. The caller still returns 200 so the sender stops
    retrying.
    """
    # Dedup check
    existing = await session.scalar(
        select(EmailEvent).where(EmailEvent.provider_event_id == provider_event_id)
    )
    if existing is not None:
        return existing, False

    # Correlate back to the original send when we have its provider id
    send_id = None
    if event.data.email_id:
        send_row = await session.scalar(
            select(EmailSend).where(EmailSend.provider_message_id == event.data.email_id)
        )
        if send_row is not None:
            send_id = send_row.id
            new_status = _status_for_event(event.type)
            if new_status is not None:
                send_row.status = new_status

    row = EmailEvent(
        provider_event_id=provider_event_id,
        email_send_id=send_id,
        provider_message_id=event.data.email_id,
        event_type=event.type,
        to_address=event.data.to[0] if event.data.to else None,
        payload=event.model_dump(mode="json"),
        received_at=clock.now(),
    )
    session.add(row)
    await session.flush()
    return row, True


def _status_for_event(event_type: str) -> str | None:
    """Map a Resend event type to a terminal `EmailSend.status` value.

    Returns None for transient/non-terminal events (e.g.
    ``email.opened``) so we don't overwrite the meaningful status that
    might already be present.
    """
    return {
        "email.delivered": "delivered",
        "email.bounced": "bounced",
        "email.complained": "complained",
        "email.failed": "failed",
    }.get(event_type)


async def list_recent_events(session: AsyncSession, *, limit: int = 100) -> list[EmailEvent]:
    """Most-recent-first event log for the operator's admin UI."""
    return list(
        await session.scalars(
            select(EmailEvent).order_by(EmailEvent.received_at.desc()).limit(limit)
        )
    )


async def record_email_send(
    session: AsyncSession,
    *,
    to: str,
    subject: str,
    provider_message_id: str | None,
    user_id: uuid.UUID | None,
    clock: Clock,
) -> EmailSend | None:
    """Persist an outbound send so later webhook events can link back.

    Callers invoke this after :meth:`EmailProvider.send` returns. When
    the provider doesn't expose an ID (``NullEmailProvider`` in dev,
    or a Resend self-host backend that returned a bare 2xx), we still
    write the row — ``provider_message_id`` is nullable — so the admin
    audit shows the send happened. Best-effort: a row-write failure
    is logged but never raised back into the caller, matching the
    "telemetry never breaks the data plane" posture used elsewhere.
    """
    try:
        row = EmailSend(
            user_id=user_id,
            to_address=to,
            subject=subject,
            provider_message_id=provider_message_id,
            status="sent",
            sent_at=clock.now(),
        )
        session.add(row)
        await session.flush()
        return row
    except Exception as exc:
        from livepeer_open_clearinghouse.providers.telemetry import (  # noqa: PLC0415
            get_logger,
        )

        get_logger(__name__).warning(
            "email_send.record_failed",
            to=to,
            subject=subject,
            provider_message_id=provider_message_id,
            error=str(exc),
        )
        return None


def now_unix() -> int:
    return int(time.time())
