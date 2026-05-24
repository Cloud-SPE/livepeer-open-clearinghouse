"""FastAPI routes for the payments domain (read-only surface).

Per exec-plan 002, the legacy ``POST /v1/payments/mint`` and
``POST /v1/usage/report`` endpoints were removed in favor of the
handoff-mode ``POST /v1/jobs`` and ``POST /v1/jobs/{id}/settle``
endpoints under ``domains/jobs/runtime.py``. What remains here is
the historical read surface — list + lookup-by-work_id — useful
for customer observability and admin audit.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from livepeer_open_clearinghouse.dependencies import (
    CurrentApiKeyDep,
    SessionDep,
)
from livepeer_open_clearinghouse.domains.payments import service
from livepeer_open_clearinghouse.domains.payments.types import (
    PaymentList,
    PaymentView,
)

router = APIRouter(prefix="/v1/payments", tags=["payments"])


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
