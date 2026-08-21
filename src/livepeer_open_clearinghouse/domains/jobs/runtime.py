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
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.errors import (
    DaemonUnavailable,
    IdempotencyOutcomeUnknown,
    OpenClearinghouseError,
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
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
    sdk_identity: Annotated[str | None, Header(alias="Livepeer-Open-Clearinghouse-SDK")] = None,
) -> CreateJobResponse:
    """Open a one-shot job. Returns broker_url + payment_envelope for
    the SDK to call the broker directly (handoff mode).

    Only routes declaring ``paid-job/v1`` are accepted.
    """
    api_key, user = pair
    api_key_id = api_key.id
    user_id = user.id
    operation = "jobs.create"
    fingerprint = payments_service.create_request_fingerprint(
        operation=operation,
        payload=body.model_dump(mode="json"),
    )
    claim = await payments_service.claim_create_request(
        db,
        user_id=user_id,
        api_key_id=api_key_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        clock=clock,
        inflight_timeout_seconds=settings.idempotency_inflight_timeout_seconds,
    )
    if claim.is_replay:
        return _replay_create_job(claim)

    try:
        response = await service.open_job(
            db,
            user_id=user_id,
            api_key_id=api_key_id,
            capability=body.capability,
            offering=body.offering,
            transport=body.transport,
            estimated_units=body.estimated_units,
            max_total_units=body.max_total_units,
            sdk_identity=sdk_identity,
            registry=registry,
            daemon=daemon,
            clock=clock,
            settings=settings,
            request_id=claim.broker_request_id,
        )
    except OpenClearinghouseError as exc:
        await db.rollback()
        if not isinstance(exc, DaemonUnavailable):
            await payments_service.fail_create_request(
                db,
                user_id=user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                http_status=exc.status_code,
                response_payload=_error_payload(exc),
                clock=clock,
                retention_seconds=settings.idempotency_retention_seconds,
                retain_tombstone=isinstance(exc, IdempotencyOutcomeUnknown),
            )
        raise

    await payments_service.complete_create_request(
        db,
        user_id=user_id,
        operation=operation,
        idempotency_key=idempotency_key,
        http_status=status.HTTP_201_CREATED,
        response_payload=response.model_dump(mode="json"),
        clock=clock,
        retention_seconds=settings.idempotency_retention_seconds,
    )
    return response


def _error_payload(exc: OpenClearinghouseError) -> dict[str, object]:
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
    }


def _replay_create_job(claim: payments_service.CreateRequestClaim) -> CreateJobResponse:
    payload = claim.replay_payload or {}
    if claim.replay_status == status.HTTP_201_CREATED:
        return CreateJobResponse.model_validate(payload)
    error = payload.get("error", {})
    if not isinstance(error, dict):
        raise RuntimeError("stored idempotency error payload is malformed")
    raise OpenClearinghouseError(
        status_code=claim.replay_status or 500,
        code=str(error.get("code", "IDEMPOTENCY_REPLAY_ERROR")),
        message=str(error.get("message", "Stored request failed")),
        details=error.get("details") if isinstance(error.get("details"), dict) else {},
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

    Verifies the broker-signed settlement against the route's pinned
    delegation and quote before changing financial state.

    Returns 404 ``job_not_found`` (unknown or wrong-owner) and 409
    ``job_already_settled`` for a second settle.
    """
    _api_key, user = pair
    return await service.settle_job(
        db,
        job_id=job_id,
        user_id=user.id,
        actual_units=body.actual_units,
        broker_job_id=body.broker_job_id,
        work_unit=body.work_unit,
        outcome=body.outcome,
        settlement=body.settlement,
        clock=clock,
        settings=settings,
    )
