"""FastAPI routes for the admin domain (operator surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from pymthouse.dependencies import (
    ClockDep,
    CurrentOperatorDep,
    SessionDep,
    SettingsDep,
)
from pymthouse.domains.admin import service
from pymthouse.domains.admin.types import (
    AdminUserList,
    AdminUserView,
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


@router.get("/users", response_model=AdminUserList)
async def list_all_users_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001 — operator auth
    db: SessionDep,
    limit: int = 100,
    offset: int = 0,
) -> AdminUserList:
    rows, total = await service.list_all_users(db, limit=limit, offset=offset)
    return AdminUserList(
        total=total,
        items=[
            AdminUserView(
                id=u.id,
                email=u.email,
                email_verified_at=u.email_verified_at,
                approved=approved,
                balance_wei=balance_wei,
                created_at=u.created_at,
            )
            for (u, approved, balance_wei) in rows
        ],
    )


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
    settings: SettingsDep,
) -> ApprovedUserView:
    try:
        approval = await service.approve_user(
            db,
            user_id=user_id,
            operator=operator,
            clock=clock,
            initial_credit_wei=settings.default_initial_credit_wei,
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
