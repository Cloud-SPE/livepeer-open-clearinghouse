"""FastAPI routes for the billing domain."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from livepeer_open_clearinghouse.dependencies import (
    CurrentOperatorDep,
    SessionDep,
    SessionUserDep,
)
from livepeer_open_clearinghouse.domains.billing import service
from livepeer_open_clearinghouse.domains.billing.types import (
    BalanceView,
    LedgerEntryView,
    LedgerPage,
    TopupRequest,
    TopupView,
)

router = APIRouter(tags=["billing"])


@router.get("/v1/accounts/me/balance", response_model=BalanceView)
async def get_my_balance(
    user: SessionUserDep,
    db: SessionDep,
) -> BalanceView:
    row = await service.get_balance(db, user_id=user.id)
    return BalanceView(user_id=row.user_id, amount_wei=row.amount_wei, updated_at=row.updated_at)


@router.get("/v1/accounts/me/ledger", response_model=LedgerPage)
async def get_my_ledger(
    user: SessionUserDep,
    db: SessionDep,
    limit: int = 50,
) -> LedgerPage:
    rows = await service.list_ledger(db, user_id=user.id, limit=limit)
    return LedgerPage(items=[LedgerEntryView.model_validate(r) for r in rows])


@router.post(
    "/v1/admin/users/{user_id}/topup",
    response_model=TopupView,
    status_code=status.HTTP_201_CREATED,
)
async def admin_topup_user(
    user_id: uuid.UUID,
    body: TopupRequest,
    operator: CurrentOperatorDep,
    db: SessionDep,
) -> TopupView:
    topup_row, balance = await service.topup(
        db,
        user_id=user_id,
        amount_wei=body.amount_wei,
        kind=body.kind,
        operator_id=operator.id,
    )
    return TopupView(
        topup_id=topup_row.id,
        new_balance_wei=balance.amount_wei,
        created_at=topup_row.created_at,
    )
