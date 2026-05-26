"""FastAPI routes for the jobs domain (handoff-mode atomic/streaming jobs).

Endpoints:

  * ``POST   /v1/jobs``                — open a job, get broker_url + envelope
  * ``POST   /v1/jobs/{id}/settle``    — report actual_units, reconcile

Cases (a) / (b) / (c) of exec-plan 002. The companion long-running
session endpoints live under ``/v1/sessions``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, status

from livepeer_open_clearinghouse.dependencies import (
    ClockDep,
    CurrentApiKeyDep,
    PaymentDaemonDep,
    RegistryDep,
    SessionDep,
    SettingsDep,
)
from livepeer_open_clearinghouse.domains.jobs import service
from livepeer_open_clearinghouse.domains.jobs.types import (
    CreateJobRequest,
    CreateJobResponse,
    SettleJobRequest,
    SettleJobResponse,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post(
    "",
    response_model=CreateJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_job_endpoint(
    body: CreateJobRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    registry: RegistryDep,
    daemon: PaymentDaemonDep,
    clock: ClockDep,
    settings: SettingsDep,
    sdk_identity: Annotated[str | None, Header(alias="Livepeer-Open-Clearinghouse-SDK")] = None,
) -> CreateJobResponse:
    """Open a one-shot job. Returns broker_url + payment_envelope for
    the SDK to call the broker directly (handoff mode).

    Modes accepted: http-reqresp@v0, http-stream@v0, http-multipart@v0.
    """
    api_key, user = pair
    return await service.open_job(
        db,
        user_id=user.id,
        api_key_id=api_key.id,
        capability=body.capability,
        offering=body.offering,
        estimated_units=body.estimated_units,
        max_total_units=body.max_total_units,
        sdk_identity=sdk_identity,
        registry=registry,
        daemon=daemon,
        clock=clock,
        settings=settings,
    )


@router.post(
    "/{job_id}/settle",
    response_model=SettleJobResponse,
)
async def settle_job_endpoint(
    job_id: uuid.UUID,
    body: SettleJobRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> SettleJobResponse:
    """Settle a job after the SDK has called the broker.

    Trusts the SDK-reported ``actual_units`` on this synchronous
    path; the reconciliation janitor verifies against the daemon
    ledger out of band.

    Returns 404 ``job_not_found`` (unknown or wrong-owner) and 409
    ``job_already_settled`` for a second settle.
    """
    _api_key, user = pair
    return await service.settle_job(
        db,
        job_id=job_id,
        user_id=user.id,
        actual_units=body.actual_units,
        outcome=body.outcome,
        settlement=body.settlement,
        clock=clock,
        settings=settings,
    )
