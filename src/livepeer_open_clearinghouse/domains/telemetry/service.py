"""Business logic for telemetry: ingest, server-side emit, retention.

Three callers in PR-1:

  - ``ingest_batch`` — runtime layer's ``POST /v1/telemetry`` handler
    calls this with a parsed batch. Per-event validation, per-event
    rejections returned alongside the accepted count.
  - ``record_server_event`` — internal helper for follow-up PRs that
    emit ``server.*`` events from LOC's own runtime. Shape-identical
    to SDK ingest but bypasses the rate limiter and accepts only the
    fields a server event would have.
  - ``purge_expired`` — retention janitor. Deletes rows whose
    ``received_ts`` is older than the configured cutoff.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.telemetry.config import (
    MAX_BATCH_SIZE,
    MAX_PAYLOAD_BYTES,
    SOURCE_SDK,
    SOURCE_SERVER,  # used by record_server_event
)
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.telemetry.types import IngestEventIn
from livepeer_open_clearinghouse.providers.clock import Clock


class TelemetryServiceError(Exception):
    code = "telemetry_error"


class BatchTooLarge(TelemetryServiceError):
    code = "batch_too_large"


class InvalidSource(TelemetryServiceError):
    code = "invalid_source"


def _validate_event(ev: IngestEventIn) -> str | None:
    """Return a rejection reason string, or None when the event is OK."""
    if not ev.event_type:
        return "event_type_empty"
    if ev.event_schema_version < 1:
        return "event_schema_version_invalid"
    # Cheap upper bound on payload size — serialize once, check bytes.
    try:
        encoded = json.dumps(ev.payload, default=str)
    except (TypeError, ValueError):
        return "payload_not_json_serializable"
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return "payload_too_large"
    return None


async def ingest_batch(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    events: list[IngestEventIn],
    clock: Clock,
) -> tuple[int, list[str]]:
    """Persist a batch of SDK-emitted events.

    Returns ``(accepted_count, rejection_reasons)``. ``rejection_reasons``
    is an ordered list matching the input — one entry per event;
    accepted events carry the empty string, rejected events carry the
    reason code.

    Raises :class:`BatchTooLarge` if the batch exceeds
    :data:`config.MAX_BATCH_SIZE`. Per-event validation failures are
    *not* raised — they're reported in the response so a buggy event
    doesn't sink the rest of the batch.
    """
    if len(events) > MAX_BATCH_SIZE:
        raise BatchTooLarge

    now = clock.now()
    reasons: list[str] = []
    rows: list[TelemetryEvent] = []
    for ev in events:
        reason = _validate_event(ev)
        if reason is not None:
            reasons.append(reason)
            continue
        reasons.append("")
        rows.append(
            TelemetryEvent(
                api_key_id=api_key_id,
                user_id=user_id,
                event_type=ev.event_type,
                event_schema_version=ev.event_schema_version,
                correlation_id=ev.correlation_id,
                client_ts=ev.client_ts,
                received_ts=now,
                source=SOURCE_SDK,
                payload=ev.payload,
            )
        )
    if rows:
        session.add_all(rows)
        await session.flush()
    accepted = sum(1 for r in reasons if r == "")
    return accepted, reasons


async def record_server_event(
    session: AsyncSession,
    *,
    event_type: str,
    event_schema_version: int,
    payload: dict[str, object],
    api_key_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    correlation_id: uuid.UUID | None,
    clock: Clock,
) -> TelemetryEvent:
    """Persist a single LOC-emitted ``server.*`` event.

    Mirror of :func:`ingest_batch` for the one-at-a-time server side.
    No rate-limiting (LOC is the source); no per-event validation
    (caller is trusted), only a sanity check that the event_type
    starts with ``server.``.
    """
    if not event_type.startswith("server."):
        raise InvalidSource(f"server events must use server.* prefix, got {event_type!r}")
    row = TelemetryEvent(
        api_key_id=api_key_id,
        user_id=user_id,
        event_type=event_type,
        event_schema_version=event_schema_version,
        correlation_id=correlation_id,
        client_ts=None,
        received_ts=clock.now(),
        source=SOURCE_SERVER,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return row


async def purge_expired(
    session: AsyncSession,
    *,
    retention_days: int,
    clock: Clock,
) -> int:
    """Delete rows with ``received_ts`` older than the retention cutoff.

    Returns the count deleted. Called by the retention janitor on its
    configured cadence.
    """
    if retention_days <= 0:
        return 0
    cutoff = clock.now() - timedelta(days=retention_days)
    result = await session.execute(
        delete(TelemetryEvent).where(TelemetryEvent.received_ts < cutoff)
    )
    return int(result.rowcount or 0)
