"""FastAPI routes for the admin domain (operator surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from pymthouse.dependencies import (
    ClockDep,
    CurrentOperatorDep,
    SessionDep,
)
from pymthouse.domains.admin import service
from pymthouse.domains.admin.types import (
    ApprovedUserView,
    PendingUserList,
    PendingUserView,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/users/pending", response_model=PendingUserList)
async def list_pending_users_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001 — operator auth
    db: SessionDep,
) -> PendingUserList:
    users = await service.list_pending_users(db)
    return PendingUserList(items=[PendingUserView.model_validate(u) for u in users])


@router.post(
    "/users/{user_id}/approve",
    response_model=ApprovedUserView,
    status_code=status.HTTP_201_CREATED,
)
async def approve_user_endpoint(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
) -> ApprovedUserView:
    try:
        approval = await service.approve_user(
            db, user_id=user_id, operator=operator, clock=clock
        )
    except service.UserNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except service.UserAlreadyApproved as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return ApprovedUserView(
        user_id=approval.user_id,
        approved_at=approval.approved_at,
        operator_id=approval.operator_id,
    )
