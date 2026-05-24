"""FastAPI routes for the sessions domain (handoff-mode session lifecycle).

Endpoints landed:

  * ``POST   /v1/sessions``                 — open a session

Endpoints still to land in subsequent Phase 2 PRs:

  * ``POST   /v1/sessions/{id}/refill``     — mint a top-up
  * ``POST   /v1/sessions/{id}/close``      — explicit close
  * ``GET    /v1/sessions/{id}``            — status / balance (customer)
  * ``POST   /v1/jobs/{id}/settle``         — single-shot settlement

See ``docs/exec-plans/active/002-long-running-sessions.md`` for the
contract each handler must satisfy.
"""

from __future__ import annotations

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
from livepeer_open_clearinghouse.domains.sessions import service
from livepeer_open_clearinghouse.domains.sessions.types import (
    CreateSessionRequest,
    CreateSessionResponse,
)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_session_endpoint(
    body: CreateSessionRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    registry: RegistryDep,
    daemon: PaymentDaemonDep,
    clock: ClockDep,
    settings: SettingsDep,
    sdk_identity: Annotated[str | None, Header(alias="Livepeer-Open-Clearinghouse-SDK")] = None,
) -> CreateSessionResponse:
    """Open a long-running session under handoff mode.

    SDK identity (the ``Livepeer-Open-Clearinghouse-SDK`` header)
    is recorded on the ``payment_session`` row for operator triage.
    Optional in v1 — non-official clients can omit it. v2 will
    enforce a min-version policy.
    """
    api_key, user = pair
    return await service.open_session(
        db,
        user_id=user.id,
        api_key_id=api_key.id,
        capability=body.capability,
        offering=body.offering,
        estimated_runway_units=body.estimated_runway_units,
        max_total_units=body.max_total_units,
        sdk_identity=sdk_identity,
        registry=registry,
        daemon=daemon,
        clock=clock,
        settings=settings,
    )
