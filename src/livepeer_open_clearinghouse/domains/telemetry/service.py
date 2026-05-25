"""Business logic for telemetry: ingest, server-side emit, retention,
customer query."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.telemetry.config import (
    MAX_BATCH_SIZE,
    MAX_PAYLOAD_BYTES,
    SOURCE_SDK,
    SOURCE_SERVER,  # used by record_server_event
)
from livepeer_open_clearinghouse.domains.telemetry.enrichment import Enrichment
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
    enrichment: Enrichment | None = None,
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

    enrich = enrichment or Enrichment()
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
                geo_region=enrich.geo_region,
                account_tier=enrich.account_tier,
                broker_operator_id=enrich.broker_operator_id,
                ingest_node_id=enrich.ingest_node_id,
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
    enrichment: Enrichment | None = None,
) -> TelemetryEvent:
    """Persist a single LOC-emitted ``server.*`` event.

    Mirror of :func:`ingest_batch` for the one-at-a-time server side.
    No rate-limiting (LOC is the source); no per-event validation
    (caller is trusted), only a sanity check that the event_type
    starts with ``server.``.
    """
    if not event_type.startswith("server."):
        raise InvalidSource(f"server events must use server.* prefix, got {event_type!r}")
    enrich = enrichment or Enrichment()
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
        geo_region=enrich.geo_region,
        account_tier=enrich.account_tier,
        broker_operator_id=enrich.broker_operator_id,
        ingest_node_id=enrich.ingest_node_id,
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


# ---------------------------------------------------------------------------
# Customer query API (GET /v1/telemetry/events)
# ---------------------------------------------------------------------------


class TelemetryWindowExpired(TelemetryServiceError):
    """``from`` or ``to`` falls outside ``TELEMETRY_RAW_RETENTION_DAYS``.

    Customers asking for older data must use the long-term store
    (v2 — NaaP). v1 returns HTTP 410 for these.
    """

    code = "telemetry_window_expired"


class InvalidCursor(TelemetryServiceError):
    code = "invalid_cursor"


def _glob_to_like(pattern: str) -> str:
    """Translate the customer-facing glob into a SQL LIKE pattern.

    Only ``*`` is supported as a wildcard — matches the documented
    contract (``request.*``, ``session.refill_*``). SQL ``%`` and
    ``_`` are escaped so they can't be smuggled in.
    """
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.replace("*", "%")


def _encode_cursor(received_ts: datetime, event_id: uuid.UUID) -> str:
    """Encode a (received_ts, id) tuple as an opaque pagination cursor.

    URL-safe base64 over an ISO-8601 timestamp + UUID, separated by
    ``|``. We don't want clients parsing this — bumping the encoding
    later doesn't break them.
    """
    raw = f"{received_ts.isoformat()}|{event_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Reverse of :func:`_encode_cursor`. Raises :class:`InvalidCursor`
    on any parse failure."""
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding).decode("ascii")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursor(f"cannot decode cursor: {exc}") from exc


def _within_retention(
    *, when: datetime, retention_days: int, now: datetime
) -> bool:
    """``when`` must be no older than the retention cutoff."""
    if retention_days <= 0:
        return True  # operator disabled retention; nothing to gate on
    return when >= now - timedelta(days=retention_days)


async def list_events_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    from_ts: datetime,
    to_ts: datetime,
    event_type_glob: str | None,
    cursor: str | None,
    page_size: int,
    retention_days: int,
    clock: Clock,
) -> tuple[list[TelemetryEvent], str | None]:
    """Portal variant of :func:`list_events_for_api_key` — aggregates
    every API key owned by ``user_id`` instead of scoping to one.

    Same window-gating + cursor semantics. The portal uses this
    via a session cookie; the SDK never sees this surface.
    """
    now = clock.now()
    if not _within_retention(when=from_ts, retention_days=retention_days, now=now):
        raise TelemetryWindowExpired(
            f"from_ts predates {retention_days}-day retention window"
        )
    if not _within_retention(when=to_ts, retention_days=retention_days, now=now):
        raise TelemetryWindowExpired(
            f"to_ts predates {retention_days}-day retention window"
        )
    if from_ts > to_ts:
        return [], None

    stmt = (
        select(TelemetryEvent)
        .where(
            TelemetryEvent.user_id == user_id,
            TelemetryEvent.received_ts >= from_ts,
            TelemetryEvent.received_ts <= to_ts,
        )
        .order_by(TelemetryEvent.received_ts.desc(), TelemetryEvent.id.desc())
        .limit(page_size + 1)
    )
    if event_type_glob is not None:
        stmt = stmt.where(
            TelemetryEvent.event_type.like(_glob_to_like(event_type_glob), escape="\\")
        )
    if cursor is not None:
        anchor_ts, anchor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                TelemetryEvent.received_ts < anchor_ts,
                and_(
                    TelemetryEvent.received_ts == anchor_ts,
                    TelemetryEvent.id < anchor_id,
                ),
            )
        )

    rows = list((await session.scalars(stmt)).all())
    if len(rows) > page_size:
        truncated = rows[:page_size]
        last = truncated[-1]
        return truncated, _encode_cursor(last.received_ts, last.id)
    return rows, None


async def list_events_for_api_key(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    from_ts: datetime,
    to_ts: datetime,
    event_type_glob: str | None,
    cursor: str | None,
    page_size: int,
    retention_days: int,
    clock: Clock,
) -> tuple[list[TelemetryEvent], str | None]:
    """Page through one API key's events within ``[from_ts, to_ts]``.

    Returns ``(rows, next_cursor)``. ``next_cursor`` is ``None`` when
    the page exhausts the result set.

    Raises :class:`TelemetryWindowExpired` if either bound predates
    the retention cutoff. Raises :class:`InvalidCursor` for malformed
    cursors.

    Ordering: ``received_ts DESC, id DESC`` (newest first). The cursor
    encodes the *last* row of the previous page; the next query asks
    for rows strictly older than that anchor.
    """
    now = clock.now()
    if not _within_retention(when=from_ts, retention_days=retention_days, now=now):
        raise TelemetryWindowExpired(
            f"from_ts predates {retention_days}-day retention window"
        )
    if not _within_retention(when=to_ts, retention_days=retention_days, now=now):
        raise TelemetryWindowExpired(
            f"to_ts predates {retention_days}-day retention window"
        )
    if from_ts > to_ts:
        # Treat as empty rather than 4xx — easier on naive clients.
        return [], None

    stmt = (
        select(TelemetryEvent)
        .where(
            TelemetryEvent.api_key_id == api_key_id,
            TelemetryEvent.received_ts >= from_ts,
            TelemetryEvent.received_ts <= to_ts,
        )
        .order_by(TelemetryEvent.received_ts.desc(), TelemetryEvent.id.desc())
        .limit(page_size + 1)  # fetch one extra to know if there's a next page
    )
    if event_type_glob is not None:
        stmt = stmt.where(
            TelemetryEvent.event_type.like(_glob_to_like(event_type_glob), escape="\\")
        )
    if cursor is not None:
        anchor_ts, anchor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                TelemetryEvent.received_ts < anchor_ts,
                and_(
                    TelemetryEvent.received_ts == anchor_ts,
                    TelemetryEvent.id < anchor_id,
                ),
            )
        )

    rows = list((await session.scalars(stmt)).all())
    if len(rows) > page_size:
        truncated = rows[:page_size]
        last = truncated[-1]
        return truncated, _encode_cursor(last.received_ts, last.id)
    return rows, None
