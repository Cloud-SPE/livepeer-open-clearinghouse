"""FastAPI routes for the admin domain (operator surface)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status

from livepeer_open_clearinghouse.dependencies import (
    ClockDep,
    CurrentOperatorDep,
    EmailDep,
    OwnerOperatorDep,
    RegistryDep,
    SessionDep,
    SettingsDep,
)
from livepeer_open_clearinghouse.domains.admin import service
from livepeer_open_clearinghouse.domains.admin.repo import Operator, OperatorAudit
from livepeer_open_clearinghouse.domains.admin.types import (
    AdminUserList,
    AdminUserView,
    ApprovedUserView,
    AuditEntryList,
    AuditEntryView,
    BillingConfigResponse,
    BillingConfigUpdate,
    BillingConfigView,
    CreateOperatorRequest,
    CreateSdkApprovalRequest,
    DepositSnapshotList,
    DepositSnapshotView,
    EffectiveBillingConfigView,
    OperatorList,
    OperatorView,
    OperatorWithToken,
    PendingUserList,
    PendingUserView,
    SdkApprovalList,
    SdkApprovalView,
    SdkDistributionEntry,
    SdkDistributionResponse,
    SdkManifest,
    SdkManifestEntry,
    SdkManifestPubkey,
    SessionWithSdkList,
    SessionWithSdkView,
    UpdateOperatorRequest,
    UpdateSdkApprovalRequest,
)
from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.discovery import service as discovery_service
from livepeer_open_clearinghouse.domains.payments import service as payments_service
from livepeer_open_clearinghouse.settings import Settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/users/pending", response_model=PendingUserList)
async def list_pending_users_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
) -> PendingUserList:
    users = await service.list_pending_users(db)
    return PendingUserList(items=[PendingUserView.model_validate(u) for u in users])


@router.get("/users", response_model=AdminUserList)
async def list_all_users_endpoint(
    operator: CurrentOperatorDep,
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
    operator: CurrentOperatorDep,
    db: SessionDep,
    settings: SettingsDep,
) -> BillingConfigResponse:
    row = await billing_service.get_billing_config(db, user_id=user_id)
    resolved = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)
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
    resolved = await billing_service.resolve_billing_config(db, user_id=user_id, settings=settings)
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
    operator: CurrentOperatorDep,
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
    operator: CurrentOperatorDep,
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
    operator: CurrentOperatorDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    """Operator view of the live capability catalog.

    Proxies through the same service layer the app-dev /v1/capabilities
    endpoint uses; the only difference is the auth model (bearer token
    instead of API-key/session). No business logic duplication.
    """
    items = await discovery_service.list_capabilities(registry)
    return {"items": [c.model_dump() for c in items]}


@router.get("/discovery/orchestrators")
async def admin_list_orchestrators_endpoint(
    operator: CurrentOperatorDep,
    registry: RegistryDep,
    capability: str | None = None,
) -> dict[str, Any]:
    items = await discovery_service.list_orchestrators(registry, capability=capability)
    return {"items": [o.model_dump() for o in items]}


# ---------------------------------------------------------------------------
# Operator management — owner-only mutations, any-operator read
# ---------------------------------------------------------------------------


def _operator_view(op: Operator) -> OperatorView:
    return OperatorView(
        id=op.id,
        email=op.email,
        name=op.name,
        role=op.role,
        last_login_at=op.last_login_at,
        revoked_at=op.revoked_at,
        created_at=op.created_at,
    )


@router.get("/operators", response_model=OperatorList)
async def list_operators_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
) -> OperatorList:
    """Any operator can list. Mutations are owner-only."""
    rows = await service.list_operators(db)
    return OperatorList(items=[_operator_view(o) for o in rows])


@router.post(
    "/operators",
    response_model=OperatorWithToken,
    status_code=status.HTTP_201_CREATED,
)
async def create_operator_endpoint(
    owner: OwnerOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    body: CreateOperatorRequest,
) -> OperatorWithToken:
    try:
        op, raw_token = await service.create_operator(
            db,
            acting_operator=owner,
            email=str(body.email),
            name=body.name,
            role=body.role,
            clock=clock,
        )
    except service.InvalidOperatorRole as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except service.OperatorEmailTaken as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return OperatorWithToken(operator=_operator_view(op), raw_token=raw_token)


@router.patch("/operators/{operator_id}", response_model=OperatorView)
async def update_operator_endpoint(
    owner: OwnerOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    operator_id: uuid.UUID,
    body: UpdateOperatorRequest,
) -> OperatorView:
    try:
        op = await service.update_operator(
            db,
            acting_operator=owner,
            operator_id=operator_id,
            name=body.name,
            role=body.role,
            clock=clock,
        )
    except service.OperatorNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except service.InvalidOperatorRole as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except service.CannotDemoteLastOwner as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return _operator_view(op)


@router.post("/operators/{operator_id}/revoke", response_model=OperatorView)
async def revoke_operator_endpoint(
    owner: OwnerOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    operator_id: uuid.UUID,
) -> OperatorView:
    try:
        op = await service.revoke_operator(
            db,
            acting_operator=owner,
            operator_id=operator_id,
            clock=clock,
        )
    except service.OperatorNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except service.CannotRevokeSelf as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except service.CannotDemoteLastOwner as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return _operator_view(op)


@router.post(
    "/operators/{operator_id}/rotate-token",
    response_model=OperatorWithToken,
)
async def rotate_operator_token_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    clock: ClockDep,
    operator_id: uuid.UUID,
) -> OperatorWithToken:
    """Owners can rotate any operator's token. Members can only rotate their own."""
    if operator.role != "owner" and operator.id != operator_id:
        raise HTTPException(status_code=403, detail="operator_role_required:owner_or_self")
    try:
        op, raw_token = await service.rotate_operator_token(
            db,
            acting_operator=operator,
            operator_id=operator_id,
            clock=clock,
        )
    except service.OperatorNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    return OperatorWithToken(operator=_operator_view(op), raw_token=raw_token)


# ---------------------------------------------------------------------------
# SDK approval list — operator-managed allow/deprecate registry
# ---------------------------------------------------------------------------


@router.get("/sdk-approvals", response_model=SdkApprovalList)
async def list_sdk_approvals_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
) -> SdkApprovalList:
    rows = await service.list_sdk_approvals(db)
    return SdkApprovalList(items=[SdkApprovalView.model_validate(r) for r in rows])


@router.post(
    "/sdk-approvals",
    response_model=SdkApprovalView,
    status_code=status.HTTP_201_CREATED,
)
async def create_sdk_approval_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    body: CreateSdkApprovalRequest,
) -> SdkApprovalView:
    try:
        row = await service.create_sdk_approval(
            db,
            acting_operator=operator,
            lang=body.lang,
            version=body.version,
            git_sha7=body.git_sha7,
            status=body.status,
            notes=body.notes,
        )
    except service.InvalidSdkApprovalStatus as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except service.SdkApprovalAlreadyExists as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return SdkApprovalView.model_validate(row)


@router.patch("/sdk-approvals/{approval_id}", response_model=SdkApprovalView)
async def update_sdk_approval_endpoint(
    approval_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
    body: UpdateSdkApprovalRequest,
) -> SdkApprovalView:
    try:
        row = await service.update_sdk_approval(
            db,
            acting_operator=operator,
            approval_id=approval_id,
            status=body.status,
            notes=body.notes,
        )
    except service.SdkApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except service.InvalidSdkApprovalStatus as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    return SdkApprovalView.model_validate(row)


@router.delete("/sdk-approvals/{approval_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sdk_approval_endpoint(
    approval_id: uuid.UUID,
    operator: CurrentOperatorDep,
    db: SessionDep,
) -> Response:
    try:
        await service.delete_sdk_approval(db, acting_operator=operator, approval_id=approval_id)
    except service.SdkApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/recent", response_model=SessionWithSdkList)
async def list_recent_sessions_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    limit: int = 100,
) -> SessionWithSdkList:
    rows = await service.list_recent_sessions_with_sdk(db, limit=limit)
    return SessionWithSdkList(
        items=[
            SessionWithSdkView(
                session_id=ps.id,
                user_id=ps.user_id,
                api_key_id=ps.api_key_id,
                work_id=ps.work_id,
                capability=ps.capability,
                offering=ps.offering,
                mode=ps.mode,
                state=ps.state,
                sdk_identity=ps.sdk_identity,
                sdk_status=status_label,
                opened_at=ps.opened_at,
                closed_at=ps.closed_at,
            )
            for (ps, status_label) in rows
        ]
    )


@router.get("/sdk-distribution", response_model=SdkDistributionResponse)
async def sdk_distribution_endpoint(
    operator: CurrentOperatorDep,
    db: SessionDep,
    limit: int = 50,
) -> SdkDistributionResponse:
    rows = await service.sdk_distribution(db, limit=limit)
    return SdkDistributionResponse(
        items=[
            SdkDistributionEntry(sdk_identity=ident, count=count, status=status_label)
            for (ident, count, status_label) in rows
        ]
    )


# ---------------------------------------------------------------------------
# Public SDK manifest — un-authed; SDKs hit it at startup
# ---------------------------------------------------------------------------

sdk_router = APIRouter(prefix="/v1/sdk", tags=["sdk"])


@sdk_router.get("/manifest", response_model=SdkManifest)
async def sdk_manifest_endpoint(
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> SdkManifest:
    """Public list of operator-approved SDK versions.

    Returns ``approved`` + ``deprecated`` rows only. Blocked rows are
    operator-internal and never published. SDKs hit this at startup
    to warn the customer if their pinned version has been deprecated;
    the response is intentionally cacheable.

    When the operator has configured ``SDK_MANIFEST_SIGNING_KEY``, the
    payload is signed and the response carries ``signature`` +
    ``key_fingerprint``. Unsigned otherwise (dev default).
    """
    rows = await service.list_approved_sdk_manifest(db)
    generated_at = clock.now()
    items = [
        SdkManifestEntry(lang=r.lang, version=r.version, git_sha7=r.git_sha7, status=r.status)
        for r in rows
    ]
    keypair = _maybe_load_signing_keypair(settings)
    if keypair is None:
        return SdkManifest(items=items, generated_at=generated_at)
    # Sign the canonical {items, generated_at} payload.
    from livepeer_open_clearinghouse.providers.signing.manifest import (  # noqa: PLC0415
        sign_payload,
    )

    canonical_payload = {
        "items": [i.model_dump() for i in items],
        "generated_at": generated_at.isoformat(),
    }
    signature = sign_payload(keypair, canonical_payload)
    return SdkManifest(
        items=items,
        generated_at=generated_at,
        signature=signature,
        key_fingerprint=keypair.fingerprint,
    )


@sdk_router.get("/manifest/pubkey", response_model=SdkManifestPubkey)
async def sdk_manifest_pubkey_endpoint(
    settings: SettingsDep,
) -> SdkManifestPubkey:
    """Operator's manifest-signing public key. SDKs fetch this once at
    startup, cache it, and use it to verify subsequent
    ``/v1/sdk/manifest`` responses offline.

    503 when signing is not configured — SDKs treat that as "unsigned
    mode, verification skipped" (acceptable for dev / single-operator
    deployments where the SDK trusts LOC directly).
    """
    keypair = _maybe_load_signing_keypair(settings)
    if keypair is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sdk_manifest_signing_not_configured",
        )
    from livepeer_open_clearinghouse.providers.signing.manifest import (  # noqa: PLC0415
        public_key_b64,
    )

    return SdkManifestPubkey(
        public_key=public_key_b64(keypair),
        key_fingerprint=keypair.fingerprint,
    )


def _maybe_load_signing_keypair(settings: Settings):  # type: ignore[no-untyped-def]
    """Decode the configured seed into a keypair, or None if unset.

    Errors are logged and treated as "unsigned" — operators set the
    seed once on deploy, and a malformed value should be visible to
    them via logs, not break the manifest endpoint.
    """
    if settings.sdk_manifest_signing_key is None:
        return None
    seed = settings.sdk_manifest_signing_key.get_secret_value()
    if not seed:
        return None
    try:
        from livepeer_open_clearinghouse.providers.signing.manifest import (  # noqa: PLC0415
            load_keypair,
        )

        return load_keypair(seed)
    except Exception:
        # Bad config; serve unsigned. Operator should see this in logs.
        return None
