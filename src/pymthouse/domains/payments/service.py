"""Business logic for payments — the headline ticket-mint orchestration.

Single function ``mint_payment`` composes:
    1. Idempotency-key reservation
    2. Route discovery via the RegistryClient
    3. Balance check (with row-locking inside billing.service)
    4. Daemon call via PaymentDaemonClient
    5. EV decrement + payment row + ledger entry
    6. Idempotency-key completion (response payload cached)

See docs/RELIABILITY.md for the state machine and the fail-closed defaults.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.billing import service as billing_service
from pymthouse.domains.payments.repo import Payment, PaymentIdempotencyKey
from pymthouse.domains.payments.types import MintPaymentResponse
from pymthouse.errors import (
    DaemonUnavailable,
    DuplicateRequest,
    InsufficientCredit,
    NoRouteAvailable,
)
from pymthouse.providers.clock import Clock
from pymthouse.providers.payment_daemon import (
    AcceptedPrice,
    CreatePaymentRequest,
    FundingIntent,
    PaymentDaemonClient,
    PaymentDaemonError,
    QuoteRef,
)
from pymthouse.providers.registry_daemon import RegistryClient


async def _check_idempotency(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    idempotency_key: str | None,
    inflight_ttl: timedelta,
    clock: Clock,
) -> MintPaymentResponse | None:
    """If a prior completed request exists, return its cached response.

    If a prior in-flight request is still within the TTL, raise DuplicateRequest.
    Otherwise returns None (caller proceeds and will register its own row).
    """
    if idempotency_key is None:
        return None

    row = await session.scalar(
        select(PaymentIdempotencyKey).where(
            PaymentIdempotencyKey.api_key_id == api_key_id,
            PaymentIdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return None

    if row.status == "completed" and row.response_payload is not None:
        return MintPaymentResponse.model_validate(row.response_payload)

    # In-flight — block replays inside the TTL.
    if row.status == "in_flight" and row.expires_at > clock.now():
        raise DuplicateRequest

    # Stale in-flight row past TTL — let the new request take over.
    return None


async def _register_idempotency_inflight(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    idempotency_key: str,
    inflight_ttl: timedelta,
    clock: Clock,
) -> None:
    """Insert / refresh an in-flight idempotency-key row."""
    existing = await session.scalar(
        select(PaymentIdempotencyKey).where(
            PaymentIdempotencyKey.api_key_id == api_key_id,
            PaymentIdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    expires = clock.now() + inflight_ttl
    if existing is None:
        session.add(
            PaymentIdempotencyKey(
                api_key_id=api_key_id,
                idempotency_key=idempotency_key,
                status="in_flight",
                expires_at=expires,
            )
        )
    else:
        existing.status = "in_flight"
        existing.expires_at = expires
        existing.response_payload = None
        existing.payment_id = None
    await session.flush()


async def _record_idempotency_completion(
    session: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    idempotency_key: str,
    payment_id: uuid.UUID,
    response: MintPaymentResponse,
) -> None:
    row = await session.scalar(
        select(PaymentIdempotencyKey).where(
            PaymentIdempotencyKey.api_key_id == api_key_id,
            PaymentIdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return
    row.status = "completed"
    row.payment_id = payment_id
    row.response_payload = response.model_dump(mode="json")


def _eth_address_to_bytes(addr: str) -> bytes:
    """Turn a ``0x``-prefixed hex address into the 20 raw bytes the daemon wants."""
    stripped = addr.removeprefix("0x")
    if len(stripped) != 40:
        raise ValueError(f"expected 40-char hex address, got {addr!r}")
    return bytes.fromhex(stripped)


async def mint_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    api_key_id: uuid.UUID,
    capability: str,
    offering: str,
    work_units: int,
    idempotency_key: str | None,
    registry: RegistryClient,
    daemon: PaymentDaemonClient,
    clock: Clock,
    inflight_ttl_seconds: int,
) -> MintPaymentResponse:
    """End-to-end ticket-mint orchestration."""
    inflight_ttl = timedelta(seconds=inflight_ttl_seconds)

    cached = await _check_idempotency(
        session,
        api_key_id=api_key_id,
        idempotency_key=idempotency_key,
        inflight_ttl=inflight_ttl,
        clock=clock,
    )
    if cached is not None:
        return cached

    if idempotency_key is not None:
        await _register_idempotency_inflight(
            session,
            api_key_id=api_key_id,
            idempotency_key=idempotency_key,
            inflight_ttl=inflight_ttl,
            clock=clock,
        )

    # 1. Discovery
    route = await registry.select(capability, offering)
    if route is None:
        raise NoRouteAvailable(capability=capability, offering=offering)

    funded_value_wei = Decimal(route.price_per_work_unit_wei) * Decimal(work_units)

    # 2. Balance check (read-only, before paying anything external)
    balance = await billing_service.get_balance(session, user_id=user_id)
    if balance.amount_wei < funded_value_wei:
        raise InsufficientCredit(
            available_wei=int(balance.amount_wei),
            required_wei=int(funded_value_wei),
        )

    # 3. Daemon call
    daemon_request = CreatePaymentRequest(
        recipient=_eth_address_to_bytes(route.eth_address),
        ticket_params_base_url=route.worker_url,
        accepted_price=AcceptedPrice(
            capability=route.capability,
            offering=route.offering,
            price_per_unit_wei=route.price_per_work_unit_wei,
            units_per_price=route.units_per_price,
            work_unit_name=route.work_unit,
            quote_ref=QuoteRef(
                quote_id=route.quote_id,
                constraint_fingerprint=route.constraint_fingerprint,
                route_fingerprint=route.route_fingerprint,
            ),
        ),
        funding=FundingIntent(
            funded_value_wei=funded_value_wei,
            estimated_units=work_units,
            max_total_units=work_units,
        ),
    )

    try:
        daemon_response = await daemon.create_payment(daemon_request)
    except PaymentDaemonError as exc:
        raise DaemonUnavailable(
            daemon="payment-daemon", reason=str(exc) or exc.__class__.__name__
        ) from exc

    # 4. Persist payment + charge balance
    payment = Payment(
        user_id=user_id,
        api_key_id=api_key_id,
        work_id=daemon_response.work_id,
        recipient_eth_address=route.eth_address,
        capability=route.capability,
        offering=route.offering,
        work_units_requested=work_units,
        price_per_work_unit_wei=Decimal(route.price_per_work_unit_wei),
        funded_value_wei=daemon_response.funded_value_wei,
        expected_value_wei=daemon_response.expected_value,
        reserved_wei=daemon_response.expected_value,
        refunded_wei=Decimal(0),
        status="issued",
    )
    session.add(payment)
    await session.flush()

    await billing_service.charge_payment(
        session,
        user_id=user_id,
        amount_wei=daemon_response.expected_value,
        payment_id=payment.id,
    )

    response = MintPaymentResponse(
        payment_id=payment.id,
        work_id=daemon_response.work_id,
        payment_bytes=base64.b64encode(daemon_response.payment_bytes).decode("ascii"),
        expected_value_wei=daemon_response.expected_value,
        funded_value_wei=daemon_response.funded_value_wei,
        recipient_eth_address=route.eth_address,
        capability=route.capability,
        offering=route.offering,
        work_units_requested=work_units,
    )

    if idempotency_key is not None:
        await _record_idempotency_completion(
            session,
            api_key_id=api_key_id,
            idempotency_key=idempotency_key,
            payment_id=payment.id,
            response=response,
        )

    return response


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def list_payments_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50
) -> list[Payment]:
    rows = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def get_payment_by_work_id(
    session: AsyncSession, *, user_id: uuid.UUID, work_id: str
) -> Payment | None:
    return await session.scalar(
        select(Payment).where(Payment.user_id == user_id, Payment.work_id == work_id)
    )
