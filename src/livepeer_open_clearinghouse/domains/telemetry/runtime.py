"""FastAPI routes for the telemetry domain.

PR-1 lands only the ingest endpoint. Subsequent PRs add the customer
query API (``GET /v1/telemetry/events``) and admin-side counters.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from livepeer_open_clearinghouse.dependencies import (
    ClockDep,
    CurrentApiKeyDep,
    SessionDep,
    SettingsDep,
    get_rate_limiter,
)
from livepeer_open_clearinghouse.domains.telemetry import service
from livepeer_open_clearinghouse.domains.telemetry.config import MAX_BATCH_SIZE
from livepeer_open_clearinghouse.domains.telemetry.enrichment import (
    EnrichmentContext,
    NoopGeoIPProvider,
    enrich,
    resolve_ingest_node_id,
)
from livepeer_open_clearinghouse.domains.telemetry.types import (
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

    enrichment_ctx = EnrichmentContext(
        source_ip=request.client.host if request.client is not None else None,
        ingest_node_id=resolve_ingest_node_id(settings.ingest_node_id),
        geoip=NoopGeoIPProvider(),
    )
    # Same enrichment for every event in this batch — they share the
    # request-level context.
    batch_enrichment = enrich(enrichment_ctx)

    try:
        accepted, reasons = await service.ingest_batch(
            db,
            api_key_id=api_key.id,
            user_id=user.id,
            events=body.events,
            clock=clock,
            enrichment=batch_enrichment,
        )
    except service.BatchTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": exc.code, "max_batch_size": MAX_BATCH_SIZE},
        ) from exc

    rejected = sum(1 for r in reasons if r)
    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        rejections=[r for r in reasons if r] if rejected else [],
    )
