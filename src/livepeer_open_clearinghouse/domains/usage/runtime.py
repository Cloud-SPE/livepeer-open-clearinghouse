"""HTTP surface for usage.

Customer routes live under ``/v1/accounts/me/usage`` and accept either the
portal session cookie or an API key. Operator routes live under
``/v1/admin`` and require an operator bearer token.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query

from livepeer_open_clearinghouse.dependencies import (
    AuthedUserDep,
    ClockDep,
    CurrentOperatorDep,
    SessionDep,
    SettingsDep,
)
from livepeer_open_clearinghouse.domains.usage import service
from livepeer_open_clearinghouse.domains.usage.types import (
    UsageAttention,
    UsageJobPage,
    UsageOverview,
    UsageSummary,
)

router = APIRouter(prefix="/v1/accounts/me/usage", tags=["usage"])
admin_router = APIRouter(prefix="/v1/admin", tags=["admin-usage"])

JobState = Literal["open", "closed"] | None


@router.get("/overview", response_model=UsageOverview)
async def my_usage_overview(
    user: AuthedUserDep, db: SessionDep, clock: ClockDep, settings: SettingsDep
) -> UsageOverview:
    """Available, held and spent credit for the current user, plus 30 days by day."""
    return await service.overview(db, user_id=user.id, clock=clock, settings=settings)


@router.get("/jobs", response_model=UsageJobPage)
async def my_usage_jobs(
    user: AuthedUserDep,
    db: SessionDep,
    clock: ClockDep,
    limit: int = Query(50, ge=1, le=service.MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    capability: str | None = None,
    offering: str | None = None,
    api_key_id: uuid.UUID | None = None,
    state: JobState = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageJobPage:
    """Paid jobs and sessions for the current user, newest first."""
    filters = service.JobFilters(
        user_id=user.id,
        capability=capability,
        offering=offering,
        api_key_id=api_key_id,
        state=state,
        since=since,
        until=until,
    )
    return await service.list_jobs(db, filters=filters, limit=limit, offset=offset, clock=clock)


@router.get("/summary", response_model=UsageSummary)
async def my_usage_summary(
    user: AuthedUserDep,
    db: SessionDep,
    clock: ClockDep,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageSummary:
    """Totals by offering, key and day for a window (default: last 30 days)."""
    return await service.summarize(db, clock=clock, user_id=user.id, since=since, until=until)


# ---------------------------------------------------------------------------
# Operator views
# ---------------------------------------------------------------------------


@admin_router.get("/users/{user_id}/usage/overview", response_model=UsageOverview)
async def admin_user_usage_overview(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> UsageOverview:
    return await service.overview(db, user_id=user_id, clock=clock, settings=settings)


@admin_router.get("/users/{user_id}/usage/jobs", response_model=UsageJobPage)
async def admin_user_usage_jobs(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    limit: int = Query(50, ge=1, le=service.MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    capability: str | None = None,
    offering: str | None = None,
    state: JobState = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageJobPage:
    filters = service.JobFilters(
        user_id=user_id,
        capability=capability,
        offering=offering,
        state=state,
        since=since,
        until=until,
    )
    return await service.list_jobs(
        db, filters=filters, limit=limit, offset=offset, clock=clock, include_user=True
    )


@admin_router.get("/users/{user_id}/usage/summary", response_model=UsageSummary)
async def admin_user_usage_summary(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageSummary:
    return await service.summarize(db, clock=clock, user_id=user_id, since=since, until=until)


@admin_router.get("/usage/summary", response_model=UsageSummary)
async def admin_fleet_usage_summary(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageSummary:
    """Fleet-wide totals with a by-user breakdown."""
    return await service.summarize(
        db, clock=clock, user_id=None, since=since, until=until, include_users=True
    )


@admin_router.get("/usage/jobs", response_model=UsageJobPage)
async def admin_fleet_usage_jobs(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    limit: int = Query(50, ge=1, le=service.MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
    user_id: uuid.UUID | None = None,
    capability: str | None = None,
    offering: str | None = None,
    state: JobState = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> UsageJobPage:
    filters = service.JobFilters(
        user_id=user_id,
        capability=capability,
        offering=offering,
        state=state,
        since=since,
        until=until,
    )
    return await service.list_jobs(
        db, filters=filters, limit=limit, offset=offset, clock=clock, include_user=True
    )


@admin_router.get("/usage/attention", response_model=UsageAttention)
async def admin_usage_attention(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    stale_after_seconds: int = Query(service.DEFAULT_STALE_AFTER_SECONDS, ge=60),
) -> UsageAttention:
    """What an operator should look at: jobs still holding funds, and refused settlements."""
    return await service.attention(db, clock=clock, stale_after_seconds=stale_after_seconds)
