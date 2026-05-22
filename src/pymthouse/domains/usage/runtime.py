"""FastAPI routes for the usage domain (app-dev surface)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from pymthouse.dependencies import CurrentApiKeyDep, SessionDep
from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.usage import service
from pymthouse.domains.usage.types import (
    ReportUsageRequest,
    UsageRecordView,
    UsageReportResponse,
)

router = APIRouter(prefix="/v1/usage", tags=["usage"])


@router.post(
    "/report",
    response_model=UsageReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def report_usage_endpoint(
    body: ReportUsageRequest,
    pair: CurrentApiKeyDep,
    db: SessionDep,
) -> UsageReportResponse:
    api_key, user = pair
    try:
        record, payment, refund_wei = await service.report_usage(
            db,
            user_id=user.id,
            api_key_id=api_key.id,
            work_id=body.work_id,
            actual_work_units=body.actual_work_units,
            request_id=body.request_id,
        )
    except service.PaymentNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc

    balance = await billing_service.get_balance(db, user_id=user.id)
    return UsageReportResponse(
        usage=UsageRecordView.model_validate(record),
        refunded_wei=refund_wei,
        payment_status=payment.status,
        new_balance_wei=balance.amount_wei,
    )
