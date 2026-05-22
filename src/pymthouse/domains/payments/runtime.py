"""FastAPI routes for the payments domain (app-dev surface)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from pymthouse.dependencies import (
    ClockDep,
    CurrentApiKeyDep,
    PaymentDaemonDep,
    RegistryDep,
    SessionDep,
    SettingsDep,
)
from pymthouse.domains.payments import service
from pymthouse.domains.payments.types import (
    MintPaymentRequest,
    MintPaymentResponse,
    PaymentList,
    PaymentView,
)

router = APIRouter(prefix="/v1/payments", tags=["payments"])


@router.post(
    "/mint",
    response_model=MintPaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def mint_payment_endpoint(
    body: MintPaymentRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
    registry: RegistryDep,
    daemon: PaymentDaemonDep,
    clock: ClockDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MintPaymentResponse:
    api_key, user = pair
    return await service.mint_payment(
        db,
        user_id=user.id,
        api_key_id=api_key.id,
        capability=body.capability,
        offering=body.offering,
        work_units=body.work_units,
        idempotency_key=idempotency_key,
        registry=registry,
        daemon=daemon,
        clock=clock,
        inflight_ttl_seconds=settings.idempotency_inflight_timeout_seconds,
    )


@router.get("/me", response_model=PaymentList)
async def list_my_payments(
    pair: CurrentApiKeyDep,
    db: SessionDep,
    limit: int = 50,
) -> PaymentList:
    _, user = pair
    rows = await service.list_payments_for_user(db, user_id=user.id, limit=limit)
    return PaymentList(items=[PaymentView.model_validate(r) for r in rows])


@router.get("/{work_id}", response_model=PaymentView)
async def get_payment_endpoint(
    work_id: str,
    pair: CurrentApiKeyDep,
    db: SessionDep,
) -> PaymentView:
    _, user = pair
    row = await service.get_payment_by_work_id(db, user_id=user.id, work_id=work_id)
    if row is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    return PaymentView.model_validate(row)
