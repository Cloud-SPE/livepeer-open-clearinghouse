"""FastAPI routes for the telemetry domain.

Two endpoints:

  - ``POST /v1/telemetry`` — SDK ingest (PR-1).
  - ``GET  /v1/telemetry/events`` — customer query (PR-4).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from livepeer_open_clearinghouse.dependencies import (
    ClockDep,
    CurrentApiKeyDep,
    CurrentOperatorDep,
    RegistryDep,
    SessionDep,
    SessionUserDep,
    SettingsDep,
    get_rate_limiter,
)
from livepeer_open_clearinghouse.domains.telemetry import service
from livepeer_open_clearinghouse.domains.telemetry.config import MAX_BATCH_SIZE
from livepeer_open_clearinghouse.domains.telemetry.enrichment import (
    Enrichment,
    EnrichmentContext,
    NoopGeoIPProvider,
    enrich,
    lookup_broker_operator,
    resolve_ingest_node_id,
)
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.telemetry.types import (
    EventList,
    EventView,
    IngestRequest,
    IngestResponse,
)
from livepeer_open_clearinghouse.providers.ratelimit import RateLimiter

router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])

_RATE_LIMIT_ROUTE = "POST /v1/telemetry"


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_endpoint(
    request: Request,
    auth: CurrentApiKeyDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    body: IngestRequest,
    registry: RegistryDep,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> IngestResponse:
    """Ingest a batch of SDK-emitted telemetry events.

    Rate-limited per API key. Batch size capped at
    :data:`config.MAX_BATCH_SIZE`. Per-event validation failures are
    returned in the response body, not raised — one bad event must
    not sink the rest of the batch.
    """
    api_key, user = auth

    # Per-API-key rate limit. Cost is one token per *event*, not per
    # request — matches the customer-facing "events/sec" semantics in
    # exec-plan 002. We pre-charge the whole batch before any DB work
    # so we never silently accept events we'd reject after persisting.
    capacity = int(settings.telemetry_ingest_rate_per_key)
    if capacity > 0:
        for _ in range(len(body.events)):
            allowed, retry_after = await limiter.acquire(
                route=_RATE_LIMIT_ROUTE,
                key=str(api_key.id),
                capacity=capacity,
                refill_per_minute=capacity * 60,
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="telemetry_rate_limited",
                    headers={"Retry-After": str(retry_after)},
                )

    # Resolve the customer's tier once per batch — Telemetry-event
    # enrichment column is reserved for this in PR-3.
    from sqlalchemy import select as _select  # noqa: PLC0415

    from livepeer_open_clearinghouse.domains.billing.repo import (  # noqa: PLC0415
        UserBillingConfig,
    )

    account_tier: str | None = await db.scalar(
        _select(UserBillingConfig.tier).where(UserBillingConfig.user_id == user.id)
    )

    enrichment_ctx = EnrichmentContext(
        source_ip=request.client.host if request.client is not None else None,
        ingest_node_id=resolve_ingest_node_id(settings.ingest_node_id),
        geoip=NoopGeoIPProvider(),
        account_tier=account_tier,
    )
    # Base enrichment shared by all events.
    batch_enrichment = enrich(enrichment_ctx)

    # Per-event override only for events with broker_url in payload —
    # used to populate broker_operator_id. Lookups go through a
    # process-wide TTL cache (registry list_orchestrators is small +
    # stable).
    event_enrichments: list[Enrichment | None] = []
    needs_override = False
    for ev in body.events:
        broker_url = (ev.payload or {}).get("broker_url")
        if isinstance(broker_url, str):
            op_id = await lookup_broker_operator(registry, broker_url)
            if op_id is not None:
                needs_override = True
                event_enrichments.append(
                    Enrichment(
                        geo_region=batch_enrichment.geo_region,
                        account_tier=batch_enrichment.account_tier,
                        broker_operator_id=op_id,
                        ingest_node_id=batch_enrichment.ingest_node_id,
                    )
                )
                continue
        event_enrichments.append(None)

    try:
        accepted, reasons = await service.ingest_batch(
            db,
            api_key_id=api_key.id,
            user_id=user.id,
            events=body.events,
            clock=clock,
            enrichment=batch_enrichment,
            event_enrichments=event_enrichments if needs_override else None,
        )
    except service.BatchTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": exc.code, "max_batch_size": MAX_BATCH_SIZE},
        ) from exc

    # Post-ingest trigger evaluation — best-effort, never raises.
    try:
        await _evaluate_post_ingest_triggers(
            db,
            api_key_id=api_key.id,
            user_id=user.id,
            events=body.events,
            clock=clock,
        )
    except Exception as exc:
        # Logged so admin can spot a misbehaving trigger; never
        # affects the ingest response.
        from livepeer_open_clearinghouse.providers.telemetry import (  # noqa: PLC0415
            get_logger,
        )

        get_logger(__name__).warning(
            "telemetry.post_ingest_triggers_failed",
            error=str(exc),
        )

    rejected = sum(1 for r in reasons if r)
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        rejections=[r for r in reasons if r] if rejected else [],
    )


# Window for the session_failed_repeatedly trigger.
_SESSION_FAILURE_WINDOW_HOURS = 1
_SESSION_FAILURE_THRESHOLD = 3


async def _evaluate_post_ingest_triggers(
    db: SessionDep,
    *,
    api_key_id: uuid.UUID,
    user_id: uuid.UUID,
    events: list,
    clock: ClockDep,
) -> None:
    """Inspect a successfully-ingested batch + fire notifications.

    Two SDK-driven triggers wire here:

      - ``quota.period_rollover`` → ``period_rollover``: SDK detected
        a spend-window cross. Fire one notification per observed
        rollover.
      - ``session.error`` → ``session_failed_repeatedly``: when the
        per-API-key error count over the last hour crosses the
        threshold, fire once. (Re-fires only after the count drops
        back below and crosses again.)
    """
    from datetime import timedelta  # noqa: PLC0415

    from livepeer_open_clearinghouse.domains.notifications import (  # noqa: PLC0415
        prefs as notification_prefs,
    )

    # period_rollover — fire on each quota.period_rollover in the batch.
    for ev in events:
        if ev.event_type == "quota.period_rollover":
            payload = ev.payload or {}
            await notification_prefs.notify_period_rollover(
                db,
                user_id=user_id,
                period_start=str(payload.get("period_start", "")),
                period_end=str(payload.get("period_end", "")),
                previous_period_spend_wei=int(payload.get("previous_period_spend_wei", 0)),
                clock=clock,
            )

    # session_failed_repeatedly — fire when the batch contains a
    # session.error AND the rolling per-key count crosses the threshold.
    has_session_error = any(ev.event_type == "session.error" for ev in events)
    if has_session_error:
        since = clock.now() - timedelta(hours=_SESSION_FAILURE_WINDOW_HOURS)
        from sqlalchemy import func, select  # noqa: PLC0415

        from livepeer_open_clearinghouse.domains.telemetry.repo import (  # noqa: PLC0415
            TelemetryEvent,
        )

        count = await db.scalar(
            select(func.count())
            .select_from(TelemetryEvent)
            .where(
                TelemetryEvent.api_key_id == api_key_id,
                TelemetryEvent.event_type == "session.error",
                TelemetryEvent.received_ts >= since,
            )
        )
        if int(count or 0) >= _SESSION_FAILURE_THRESHOLD:
            await notification_prefs.notify_session_failed_repeatedly(
                db,
                user_id=user_id,
                api_key_id=api_key_id,
                failure_count=int(count or 0),
                window_hours=_SESSION_FAILURE_WINDOW_HOURS,
                clock=clock,
            )


# ---------------------------------------------------------------------------
# GET /v1/telemetry/events — customer query
# ---------------------------------------------------------------------------


_QUERY_RATE_LIMIT_ROUTE = "GET /v1/telemetry/events"


def _event_view(row: TelemetryEvent) -> EventView:
    return EventView(
        id=row.id,
        event_type=row.event_type,
        event_schema_version=row.event_schema_version,
        correlation_id=row.correlation_id,
        client_ts=row.client_ts,
        received_ts=row.received_ts,
        source=row.source,
        payload=row.payload,
    )


@router.get("/events", response_model=None)
async def query_events_endpoint(
    auth: CurrentApiKeyDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    from_ts: Annotated[
        str, Query(alias="from", description="ISO-8601 lower bound on received_ts.")
    ],
    to_ts: Annotated[
        str, Query(alias="to", description="ISO-8601 upper bound on received_ts.")
    ],
    type_glob: Annotated[
        str | None,
        Query(
            alias="type",
            description="Glob over event_type. Only `*` is a wildcard.",
        ),
    ] = None,
    fmt: Annotated[Literal["json", "ndjson"], Query(alias="format")] = "json",
    cursor: Annotated[str | None, Query()] = None,
    page_size: Annotated[int | None, Query(ge=1, le=5000)] = None,
) -> EventList | StreamingResponse:
    """List the calling API key's telemetry events within ``[from, to]``.

    Scoped strictly to the calling key — customers cannot see another
    customer's events. ``from``/``to`` must fall within
    ``TELEMETRY_RAW_RETENTION_DAYS``; older windows return 410.
    ``format=ndjson`` streams one JSON object per line for large
    downloads.
    """
    api_key, _user = auth

    # Per-API-key rate limit on read.
    capacity = int(settings.telemetry_query_rate_per_key_per_minute)
    if capacity > 0:
        allowed, retry_after = await limiter.acquire(
            route=_QUERY_RATE_LIMIT_ROUTE,
            key=str(api_key.id),
            capacity=capacity,
            refill_per_minute=capacity,
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="telemetry_query_rate_limited",
                headers={"Retry-After": str(retry_after)},
            )

    # Parse the timestamps cooperatively — empty string or malformed
    # → 400 with a clear message rather than a Pydantic blob.
    # Naive timestamps are treated as UTC.
    from datetime import UTC, datetime  # noqa: PLC0415

    try:
        from_dt = datetime.fromisoformat(from_ts)
        to_dt = datetime.fromisoformat(to_ts)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_timestamp", "message": str(exc)},
        ) from exc
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=UTC)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=UTC)

    effective_page_size = page_size or min(
        100, int(settings.telemetry_query_max_page_size)
    )

    try:
        rows, next_cursor = await service.list_events_for_api_key(
            db,
            api_key_id=api_key.id,
            from_ts=from_dt,
            to_ts=to_dt,
            event_type_glob=type_glob,
            cursor=cursor,
            page_size=effective_page_size,
            retention_days=int(settings.telemetry_raw_retention_days),
            clock=clock,
        )
    except service.TelemetryWindowExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retention_days": int(settings.telemetry_raw_retention_days),
            },
        ) from exc
    except service.InvalidCursor as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    if fmt == "ndjson":
        async def _stream() -> AsyncIterator[bytes]:
            for row in rows:
                line = _event_view(row).model_dump_json()
                yield (line + "\n").encode("utf-8")
            # Trailing cursor record so streaming clients know where
            # to resume without an extra round-trip.
            yield (
                json.dumps({"_cursor": next_cursor}, default=str) + "\n"
            ).encode("utf-8")

        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    return EventList(
        items=[_event_view(r) for r in rows],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# Portal-cookie-authenticated surface (`/v1/accounts/me/telemetry/*`)
#
# Same query semantics as the SDK-facing endpoint but scoped by user
# rather than API key, and authed via the session cookie set by
# /v1/auth/login. Powers the portal Telemetry tab.
# ---------------------------------------------------------------------------


portal_router = APIRouter(prefix="/v1/accounts/me/telemetry", tags=["telemetry"])


def _parse_window_or_400(
    from_ts: str, to_ts: str
) -> tuple[datetime, datetime]:
    from datetime import UTC  # noqa: PLC0415

    try:
        from_dt = datetime.fromisoformat(from_ts)
        to_dt = datetime.fromisoformat(to_ts)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_timestamp", "message": str(exc)},
        ) from exc
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=UTC)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=UTC)
    return from_dt, to_dt


@portal_router.get("/events", response_model=EventList)
async def portal_query_events_endpoint(
    user: SessionUserDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    from_ts: Annotated[str, Query(alias="from")],
    to_ts: Annotated[str, Query(alias="to")],
    type_glob: Annotated[str | None, Query(alias="type")] = None,
    cursor: Annotated[str | None, Query()] = None,
    page_size: Annotated[int | None, Query(ge=1, le=5000)] = None,
) -> EventList:
    """Customer-facing event listing — aggregates across every API key
    the user owns. Session-cookie auth; the SDK uses the per-key
    variant at ``/v1/telemetry/events``."""
    from_dt, to_dt = _parse_window_or_400(from_ts, to_ts)
    effective_page_size = page_size or min(
        100, int(settings.telemetry_query_max_page_size)
    )
    try:
        rows, next_cursor = await service.list_events_for_user(
            db,
            user_id=user.id,
            from_ts=from_dt,
            to_ts=to_dt,
            event_type_glob=type_glob,
            cursor=cursor,
            page_size=effective_page_size,
            retention_days=int(settings.telemetry_raw_retention_days),
            clock=clock,
        )
    except service.TelemetryWindowExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retention_days": int(settings.telemetry_raw_retention_days),
            },
        ) from exc
    except service.InvalidCursor as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return EventList(
        items=[_event_view(r) for r in rows],
        next_cursor=next_cursor,
    )


@portal_router.get("/download")
async def portal_download_endpoint(
    user: SessionUserDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """30-day NDJSON download — full event log for the user across
    every API key. Streams to support large windows without buffering
    in memory."""
    from datetime import UTC, timedelta  # noqa: PLC0415

    retention = int(settings.telemetry_raw_retention_days)
    now = clock.now()
    to_dt = now
    # Window = retention days OR 30, whichever is smaller.
    window_days = min(retention, 30) if retention > 0 else 30
    from_dt = now - timedelta(days=window_days)
    # Naive → UTC.
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=UTC)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=UTC)
    page_size = int(settings.telemetry_query_max_page_size)

    async def _stream() -> AsyncIterator[bytes]:
        cursor: str | None = None
        while True:
            rows, next_cursor = await service.list_events_for_user(
                db,
                user_id=user.id,
                from_ts=from_dt,
                to_ts=to_dt,
                event_type_glob=None,
                cursor=cursor,
                page_size=page_size,
                retention_days=retention,
                clock=clock,
            )
            for row in rows:
                yield (_event_view(row).model_dump_json() + "\n").encode("utf-8")
            if next_cursor is None:
                break
            cursor = next_cursor

    today = clock.now().date().isoformat()
    headers = {
        "Content-Disposition": f'attachment; filename="telemetry-{today}.ndjson"',
    }
    return StreamingResponse(
        _stream(), media_type="application/x-ndjson", headers=headers
    )


# ---------------------------------------------------------------------------
# Public privacy notice
# ---------------------------------------------------------------------------


privacy_router = APIRouter(prefix="/v1/privacy", tags=["privacy"])


@privacy_router.get("/telemetry")
async def privacy_notice_endpoint(
    settings: SettingsDep,
) -> dict[str, Any]:
    """Static privacy notice for the telemetry pipeline. Exposed
    un-authed so customers (and their compliance teams) can read it
    before signing up."""
    return {
        "version": "1.0",
        "effective_date": "2026-05-25",
        "categories_collected": [
            "request lifecycle timing + status codes",
            "session lifecycle events + outcomes",
            "billing events (refill granted/denied, cap_status snapshots)",
            "SDK identity (lang, version, git sha)",
        ],
        "categories_not_collected": [
            "request body content (prompts, payloads, frames)",
            "response body content (completions, outputs, media)",
            "any customer-identifiable content beyond the api_key_id",
        ],
        "retention_days": int(settings.telemetry_raw_retention_days),
        "lawful_basis": "Performance of the contract — telemetry is "
        "necessary to operate the billable service, including SLA "
        "enforcement, incident response, and dispute resolution.",
        "data_subject_rights": (
            "Customers can request deletion of their telemetry under "
            "data-subject-access rights via the operator's admin "
            "surface. Aggregated metrics that have been computed and "
            "no longer carry individual identifiers may remain."
        ),
        "contact": "privacy@livepeer-open-clearinghouse.local",
    }


# ---------------------------------------------------------------------------
# Admin SPA real-time panels
#
# Aggregates over the Postgres window — cheap GROUP BYs against the
# (event_type, received_ts) and (api_key_id, received_ts) indexes.
# Operator-bearer auth; the data covers the full retention window
# regardless of who emitted what.
# ---------------------------------------------------------------------------


admin_router = APIRouter(prefix="/v1/admin/telemetry", tags=["telemetry-admin"])


@admin_router.get("/event-counts")
async def admin_event_counts_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
) -> dict[str, Any]:
    """Per-event-type counts over the last ``hours`` window.

    Surfaces the operator-facing real-time roll-ups documented in
    exec-plan 002 — server.refill_denied, server.mint_refused,
    server.discrepancy_detected, server.sdk_sha_mismatch, *.error
    rates.
    """
    from datetime import timedelta  # noqa: PLC0415

    since = clock.now() - timedelta(hours=hours)
    counts = await service.admin_event_counts(db, since=since, clock=clock)
    _ = operator  # surface only — RBAC via CurrentOperatorDep
    return {"window_hours": hours, "counts": counts}


@admin_router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_200_OK,
)
async def admin_purge_user_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """DSAR (data-subject access-rights) purge: hard-delete every
    ``telemetry_event`` row for the given user. The operator writes
    an operator_audit row alongside so we can answer "when was this
    deletion performed and by whom" later. Idempotent."""
    from livepeer_open_clearinghouse.domains.admin.repo import (  # noqa: PLC0415
        OperatorAudit,
    )

    deleted = await service.purge_for_user(db, user_id=user_id)
    db.add(
        OperatorAudit(
            operator_id=operator.id,
            action="purge_user_telemetry",
            target_user_id=user_id,
            params={"deleted": deleted},
        )
    )
    return {"user_id": str(user_id), "deleted": deleted}


@admin_router.get("/rate-limit-offenders")
async def admin_rate_limit_offenders_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Top ``limit`` API keys by event count in the last ``hours`` —
    helps spot a misbehaving SDK or undertuned per-key cap."""
    from datetime import timedelta  # noqa: PLC0415

    since = clock.now() - timedelta(hours=hours)
    rows = await service.admin_rate_limit_offenders(db, since=since, limit=limit)
    _ = operator
    return {
        "window_hours": hours,
        "items": [
            {"api_key_id": str(api_key_id) if api_key_id else None, "count": count}
            for (api_key_id, count) in rows
        ],
    }
