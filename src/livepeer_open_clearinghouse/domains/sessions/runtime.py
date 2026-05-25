"""FastAPI routes for the sessions domain (handoff-mode session lifecycle).

Endpoints landed:

  * ``POST   /v1/sessions``                 — open a session
  * ``POST   /v1/sessions/{id}/refill``     — mint a top-up
  * ``POST   /v1/sessions/{id}/close``      — explicit close
  * ``GET    /v1/sessions/{id}``            — status / balance (customer)

Endpoints still to land in subsequent Phase 2 PRs:

  * ``POST   /v1/jobs/{id}/settle``         — single-shot settlement

See ``docs/exec-plans/active/002-long-running-sessions.md`` for the
contract each handler must satisfy.
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
from livepeer_open_clearinghouse.domains.sessions import service
from livepeer_open_clearinghouse.domains.sessions.types import (
    CloseSessionRequest,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    RefillSessionRequest,
    RefillSessionResponse,
    SessionStatusResponse,
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


@router.post(
    "/{session_id}/refill",
    response_model=RefillSessionResponse,
)
async def refill_session_endpoint(
    session_id: uuid.UUID,
    body: RefillSessionRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    daemon: PaymentDaemonDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> RefillSessionResponse:
    """Mint a top-up envelope bound to an existing session.

    Returns 400 ``refill_not_supported_for_mode`` for (d-bounded)
    sessions (``ws-realtime@v0`` — no protocol topup). Returns 402
    ``cap_reached`` when a session / spend-period cap is hit.
    Returns 409 ``session_not_open`` when the session is in
    ``draining`` or ``closed`` state.
    """
    api_key, user = pair
    return await service.refill_session(
        db,
        session_id=session_id,
        user_id=user.id,
        api_key_id=api_key.id,
        observed_consumed_units=body.observed_consumed_units,
        daemon=daemon,
        clock=clock,
        settings=settings,
    )


@router.get(
    "/{session_id}",
    response_model=SessionStatusResponse,
)
async def get_session_status_endpoint(
    session_id: uuid.UUID,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> SessionStatusResponse:
    """Customer-facing snapshot of a session's state + accounting.

    Returns 404 ``session_not_found`` for unknown sessions or
    sessions owned by another user (uniform — does not disclose
    existence).
    """
    _api_key, user = pair
    return await service.get_session_status(
        db,
        session_id=session_id,
        user_id=user.id,
        clock=clock,
        settings=settings,
    )


@router.post(
    "/{session_id}/close",
    response_model=CloseSessionResponse,
)
async def close_session_endpoint(
    session_id: uuid.UUID,
    body: CloseSessionRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    clock: ClockDep,
    daemon: PaymentDaemonDep,
) -> CloseSessionResponse:
    """Explicitly close a session and finalize accounting.

    Trusts the SDK-reported ``actual_units`` on this synchronous
    path. The reconciliation janitor (PR-8) does the daemon
    cross-check via ``GetSessionDebits`` and corrects divergence.

    Returns 409 ``session_not_open`` for an already-closed session
    (idempotency note: a second close is rejected, not a no-op —
    the SDK should treat the first 200 as authoritative).
    """
    _api_key, user = pair
    return await service.close_session(
        db,
        session_id=session_id,
        user_id=user.id,
        actual_units=body.actual_units,
        outcome=body.outcome,
        settlement=body.settlement,
        clock=clock,
        daemon=daemon,
    )
