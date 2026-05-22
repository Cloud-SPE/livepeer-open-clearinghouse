"""Business logic for usage reconciliation.

App devs report actuals via ``POST /v1/usage/report``. We compute
``actual_cost = actual_units × price`` and refund the delta against the
reserved amount on the payment. First-write-wins via the
``UNIQUE(api_key_id, payment_id)`` constraint on `usage_record`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.payments.repo import Payment
from pymthouse.domains.usage.repo import UsageRecord


class UsageServiceError(Exception):
    code = "usage_error"


class PaymentNotFound(UsageServiceError):
    code = "payment_not_found"


async def report_usage(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    work_id: str,
    actual_work_units: int,
    request_id: str | None,
) -> tuple[UsageRecord, Payment, Decimal]:
    """Reconcile a payment against reported actuals.

    Returns ``(record, payment, refund_wei)``. If a usage_record for this
    `(api_key_id, payment_id)` already exists, returns it unchanged (no
    additional refund).
    """
    payment = await session.scalar(
        select(Payment).where(Payment.user_id == user_id, Payment.work_id == work_id)
    )
    if payment is None:
        raise PaymentNotFound

    existing = await session.scalar(
        select(UsageRecord).where(
            UsageRecord.api_key_id == api_key_id,
            UsageRecord.payment_id == payment.id,
        )
    )
    if existing is not None:
        return existing, payment, Decimal(0)

    actual_cost = (
        Decimal(actual_work_units) * payment.price_per_work_unit_wei
    )
    # Refund is (reserved - already-refunded) - actual. Clamped to >= 0.
    reservation_remaining = payment.reserved_wei - payment.refunded_wei
    refund = reservation_remaining - actual_cost
    if refund < 0:
        refund = Decimal(0)

    record = UsageRecord(
        payment_id=payment.id,
        api_key_id=api_key_id,
        user_id=user_id,
        actual_work_units=actual_work_units,
        actual_cost_wei=actual_cost,
        request_id=request_id,
    )
    session.add(record)
    await session.flush()

    if refund > 0:
        await billing_service.refund_payment(
            session, user_id=user_id, amount_wei=refund, payment_id=payment.id
        )
        payment.refunded_wei = payment.refunded_wei + refund

    payment.status = "reconciled"
    return record, payment, refund
