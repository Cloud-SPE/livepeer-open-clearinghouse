"""FastAPI routes for the admin domain (operator surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status

from pymthouse.dependencies import (
    ClockDep,
    CurrentOperatorDep,
    EmailDep,
    RegistryDep,
    SessionDep,
    SettingsDep,
)
from pymthouse.domains.admin import service
from pymthouse.domains.admin.repo import OperatorAudit
from pymthouse.domains.admin.types import (
    AdminUserList,
    AdminUserView,
    ApprovedUserView,
    AuditEntryList,
    AuditEntryView,
    BillingConfigResponse,
    BillingConfigUpdate,
    BillingConfigView,
    DepositSnapshotList,
    DepositSnapshotView,
    EffectiveBillingConfigView,
    PendingUserList,
    PendingUserView,
)
from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.discovery import service as discovery_service
from pymthouse.domains.payments import service as payments_service

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
    email: EmailDep,
) -> ApprovedUserView:
    try:
        approval = await service.approve_user(
            db,
            user_id=user_id,
            operator=operator,
            clock=clock,
            initial_credit_wei=settings.default_initial_credit_wei,
            email_provider=email,
            public_base_url=str(settings.public_base_url),
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


def _config_view(row: object | None, user_id: uuid.UUID) -> BillingConfigView:
    if row is None:
        return BillingConfigView(
            user_id=user_id,
            spend_period_seconds=None,
            spend_period_cap_wei=None,
            auto_replenish_increment_wei=None,
            auto_replenish_threshold_wei=None,
        )
    return BillingConfigView(
        user_id=row.user_id,  # type: ignore[attr-defined]
        spend_period_seconds=row.spend_period_seconds,  # type: ignore[attr-defined]
        spend_period_cap_wei=(
            int(row.spend_period_cap_wei)  # type: ignore[attr-defined]
            if row.spend_period_cap_wei is not None  # type: ignore[attr-defined]
            else None
        ),
        auto_replenish_increment_wei=(
            int(row.auto_replenish_increment_wei)  # type: ignore[attr-defined]
            if row.auto_replenish_increment_wei is not None  # type: ignore[attr-defined]
            else None
        ),
        auto_replenish_threshold_wei=(
            int(row.auto_replenish_threshold_wei)  # type: ignore[attr-defined]
            if row.auto_replenish_threshold_wei is not None  # type: ignore[attr-defined]
            else None
        ),
    )


@router.get(
    "/users/{user_id}/billing-config",
    response_model=BillingConfigResponse,
)
async def get_billing_config_endpoint(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,  # noqa: ARG001
    db: SessionDep,
    settings: SettingsDep,
) -> BillingConfigResponse:
    row = await billing_service.get_billing_config(db, user_id=user_id)
    resolved = await billing_service.resolve_billing_config(
        db, user_id=user_id, settings=settings
    )
    return BillingConfigResponse(
        config=_config_view(row, user_id),
        effective=EffectiveBillingConfigView(
            spend_period_seconds=resolved.spend_period_seconds,
            spend_period_cap_wei=resolved.spend_period_cap_wei,
            auto_replenish_increment_wei=resolved.auto_replenish_increment_wei,
            auto_replenish_threshold_wei=resolved.auto_replenish_threshold_wei,
        ),
    )


@router.put(
    "/users/{user_id}/billing-config",
    response_model=BillingConfigResponse,
)
async def put_billing_config_endpoint(
    user_id: uuid.UUID,
    body: BillingConfigUpdate,
    operator: CurrentOperatorDep,
    db: SessionDep,
    settings: SettingsDep,
) -> BillingConfigResponse:
    await billing_service.upsert_billing_config(
        db,
        user_id=user_id,
        operator_id=operator.id,
        spend_period_seconds=body.spend_period_seconds,
        spend_period_cap_wei=body.spend_period_cap_wei,
        auto_replenish_increment_wei=body.auto_replenish_increment_wei,
        auto_replenish_threshold_wei=body.auto_replenish_threshold_wei,
    )
    db.add(
        OperatorAudit(
            operator_id=operator.id,
            action="update_billing_config",
            target_user_id=user_id,
            params=body.model_dump(mode="json"),
        )
    )
    row = await billing_service.get_billing_config(db, user_id=user_id)
    resolved = await billing_service.resolve_billing_config(
        db, user_id=user_id, settings=settings
    )
    return BillingConfigResponse(
        config=_config_view(row, user_id),
        effective=EffectiveBillingConfigView(
            spend_period_seconds=resolved.spend_period_seconds,
            spend_period_cap_wei=resolved.spend_period_cap_wei,
            auto_replenish_increment_wei=resolved.auto_replenish_increment_wei,
            auto_replenish_threshold_wei=resolved.auto_replenish_threshold_wei,
        ),
    )


@router.get("/deposit-snapshots", response_model=DepositSnapshotList)
async def list_deposit_snapshots_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001
    db: SessionDep,
    limit: int = 100,
) -> DepositSnapshotList:
    rows = await payments_service.list_deposit_snapshots(db, limit=limit)
    return DepositSnapshotList(
        items=[
            DepositSnapshotView(
                id=r.id,
                taken_at=r.taken_at,
                deposit_wei=int(r.deposit_wei),
                reserve_wei=int(r.reserve_wei),
                withdraw_round=r.withdraw_round,
            )
            for r in rows
        ]
    )


@router.get("/audit", response_model=AuditEntryList)
async def list_audit_entries_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001
    db: SessionDep,
    limit: int = 100,
) -> AuditEntryList:
    rows = await service.list_audit_entries(db, limit=limit)
    return AuditEntryList(
        items=[
            AuditEntryView(
                id=audit.id,
                operator_email=op_email,
                action=audit.action,
                target_user_email=target_email,
                target_user_id=audit.target_user_id,
                params=audit.params,
                created_at=audit.created_at,
            )
            for (audit, op_email, target_email) in rows
        ]
    )


@router.post(
    "/users/{user_id}/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_verification_endpoint(
    user_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    email: EmailDep,
    settings: SettingsDep,
) -> Response:
    """Re-send the email-verification message to an unverified user."""
    try:
        await service.resend_verification(
            db,
            user_id=user_id,
            operator=operator,
            clock=clock,
            email_provider=email,
            public_base_url=str(settings.public_base_url),
        )
    except service.UserNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except service.EmailAlreadyVerified as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/discovery/capabilities")
async def admin_list_capabilities_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001
    registry: RegistryDep,
) -> dict:
    """Operator view of the live capability catalog.

    Proxies through the same service layer the app-dev /v1/capabilities
    endpoint uses; the only difference is the auth model (bearer token
    instead of API-key/session). No business logic duplication.
    """
    items = await discovery_service.list_capabilities(registry)
    return {"items": [c.model_dump() for c in items]}


@router.get("/discovery/orchestrators")
async def admin_list_orchestrators_endpoint(
    operator: CurrentOperatorDep,  # noqa: ARG001
    registry: RegistryDep,
    capability: str | None = None,
) -> dict:
    items = await discovery_service.list_orchestrators(
        registry, capability=capability
    )
    return {"items": [o.model_dump() for o in items]}
