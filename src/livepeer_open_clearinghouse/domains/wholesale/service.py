"""Planning and request construction for aggregate wholesale funding."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.wholesale.repo import (
    WholesaleAccount,
    WholesaleExposureBudget,
    WholesaleFunding,
)
from livepeer_open_clearinghouse.domains.wholesale.types import (
    WholesaleFundingLimits,
    WholesaleFundingPlan,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    WholesaleAccountObservation,
    WholesaleFundingResult,
)
from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    AccountFundingIntent,
    CreatePaymentRequest,
    CreatePaymentResponse,
    FundingIntent,
    QuoteRef,
)
from livepeer_open_clearinghouse.providers.registry_daemon import SelectedRoute

_ETH_ADDRESS_BYTES = 20


class WholesaleFundingPolicyError(ValueError):
    """Funding would exceed an operator-controlled exposure boundary."""


async def claim_account_funding(
    db: AsyncSession,
    *,
    route: SelectedRoute,
    observation: WholesaleAccountObservation,
    plan: WholesaleFundingPlan,
    limits: WholesaleFundingLimits,
    mint_request_id: str,
    correlation_id: str | None,
    protocol_version: str,
) -> WholesaleFunding | None:
    """Durably claim one exact mint intent before calling the daemon.

    This commits intentionally. Callers must use a dedicated clean session so
    an external mint can never precede its replay record.
    """

    if db.new or db.dirty or db.deleted:
        raise WholesaleFundingPolicyError("funding claim requires a clean database session")
    existing = await db.scalar(
        select(WholesaleFunding).where(WholesaleFunding.mint_request_id == mint_request_id)
    )
    if existing is not None:
        recorded = (
            existing.target_available_wei,
            existing.observed_available_wei,
            existing.requested_shortfall_wei,
            existing.route_snapshot,
        )
        requested = (
            plan.target_available_wei,
            plan.observed_available_wei,
            plan.shortfall_wei,
            route.snapshot(),
        )
        if recorded != requested:
            raise WholesaleFundingPolicyError("mint_request_id replay changed funding scope")
        await db.commit()
        return existing
    budget = await db.scalar(
        select(WholesaleExposureBudget)
        .where(WholesaleExposureBudget.scope == "global")
        .with_for_update()
    )
    if budget is None:
        raise WholesaleFundingPolicyError("wholesale exposure budget is not initialized")
    account = await db.scalar(
        select(WholesaleAccount)
        .where(
            WholesaleAccount.chain_id == observation.chain_id,
            WholesaleAccount.payer_eth_address == observation.payer,
            WholesaleAccount.payee_eth_address == observation.payee,
            WholesaleAccount.denomination == observation.denomination,
        )
        .with_for_update()
    )
    prior_available = Decimal(0) if account is None else account.available_value_wei
    synchronized_aggregate = (
        budget.projected_available_wei - prior_available + observation.available_value_wei
    )
    locked_plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=synchronized_aggregate,
        limits=limits,
    )
    if locked_plan != plan:
        raise WholesaleFundingPolicyError("funding plan changed while acquiring exposure lock")
    if account is None:
        account = WholesaleAccount(
            chain_id=observation.chain_id,
            payer_eth_address=observation.payer,
            payee_eth_address=observation.payee,
            denomination=observation.denomination,
            protocol_version=protocol_version,
            broker_url=route.worker_url,
            credited_value_wei=observation.credited_value_wei,
            reserved_value_wei=observation.reserved_value_wei,
            debited_value_wei=observation.debited_value_wei,
            available_value_wei=observation.available_value_wei,
            remote_version=observation.version,
            observed_at=observation.observed_at,
        )
        db.add(account)
        await db.flush()
    elif observation.version < account.remote_version:
        raise WholesaleFundingPolicyError("broker account observation moved backwards")
    else:
        account.protocol_version = protocol_version
        account.broker_url = route.worker_url
        account.credited_value_wei = observation.credited_value_wei
        account.reserved_value_wei = observation.reserved_value_wei
        account.debited_value_wei = observation.debited_value_wei
        account.available_value_wei = observation.available_value_wei
        account.remote_version = observation.version
        account.observed_at = observation.observed_at
    budget.projected_available_wei = locked_plan.projected_aggregate_available_wei
    if plan.shortfall_wei == 0:
        await db.commit()
        return None
    funding = WholesaleFunding(
        account_id=account.id,
        mint_request_id=mint_request_id,
        correlation_id=correlation_id,
        target_available_wei=plan.target_available_wei,
        observed_available_wei=plan.observed_available_wei,
        requested_shortfall_wei=plan.shortfall_wei,
        route_snapshot=route.snapshot(),
        payment_bytes=None,
        minted_expected_value_wei=Decimal(0),
        credited_value_wei=None,
        work_id=None,
        account_version=None,
        status="claimed",
        acknowledged_at=None,
    )
    db.add(funding)
    await db.commit()
    return funding


async def record_minted_funding(
    db: AsyncSession, *, mint_request_id: str, response: CreatePaymentResponse
) -> WholesaleFunding:
    """Persist the exact replayable envelope before submitting it to a broker."""

    funding = await db.scalar(
        select(WholesaleFunding)
        .where(WholesaleFunding.mint_request_id == mint_request_id)
        .with_for_update()
    )
    if funding is None or response.expected_value != funding.requested_shortfall_wei:
        raise WholesaleFundingPolicyError("mint result does not match its durable claim")
    if funding.payment_bytes is not None and funding.payment_bytes != response.payment_bytes:
        raise WholesaleFundingPolicyError("mint replay changed payment bytes")
    funding.payment_bytes = response.payment_bytes
    funding.minted_expected_value_wei = response.expected_value
    funding.work_id = response.work_id or None
    if funding.status != "acknowledged":
        funding.status = "minted"
    await db.commit()
    return funding


async def acknowledge_account_funding(
    db: AsyncSession,
    *,
    mint_request_id: str,
    result: WholesaleFundingResult,
    observation: WholesaleAccountObservation,
    acknowledged_at: datetime,
) -> WholesaleFunding:
    """Converge a replayable broker credit onto LOC's account snapshot."""

    funding = await db.scalar(
        select(WholesaleFunding)
        .where(WholesaleFunding.mint_request_id == mint_request_id)
        .with_for_update()
    )
    if funding is None or funding.payment_bytes is None:
        raise WholesaleFundingPolicyError("broker acknowledgement has no minted funding claim")
    account = await db.scalar(
        select(WholesaleAccount).where(WholesaleAccount.id == funding.account_id).with_for_update()
    )
    if account is None:
        raise WholesaleFundingPolicyError("funding account no longer exists")
    budget = await db.scalar(
        select(WholesaleExposureBudget)
        .where(WholesaleExposureBudget.scope == "global")
        .with_for_update()
    )
    if budget is None:
        raise WholesaleFundingPolicyError("wholesale exposure budget is not initialized")
    if (
        result.payer != account.payer_eth_address
        or result.payee != account.payee_eth_address
        or observation.payer != account.payer_eth_address
        or observation.payee != account.payee_eth_address
    ):
        raise WholesaleFundingPolicyError("broker acknowledgement changed account identity")
    if result.credited_value_wei != funding.requested_shortfall_wei:
        raise WholesaleFundingPolicyError("broker acknowledgement changed credited value")
    if result.account_version != observation.version:
        raise WholesaleFundingPolicyError("broker acknowledgement and observation disagree")
    if observation.version < account.remote_version:
        raise WholesaleFundingPolicyError("broker account observation moved backwards")
    already_acknowledged = funding.status == "acknowledged"
    projected_for_account = account.available_value_wei
    if not already_acknowledged:
        projected_for_account += funding.requested_shortfall_wei
    budget.projected_available_wei = (
        budget.projected_available_wei - projected_for_account + observation.available_value_wei
    )
    account.credited_value_wei = observation.credited_value_wei
    account.reserved_value_wei = observation.reserved_value_wei
    account.debited_value_wei = observation.debited_value_wei
    account.available_value_wei = observation.available_value_wei
    account.remote_version = observation.version
    account.observed_at = observation.observed_at
    funding.credited_value_wei = result.credited_value_wei
    funding.account_version = result.account_version
    funding.status = "acknowledged"
    funding.acknowledged_at = acknowledged_at
    await db.commit()
    return funding


def plan_account_shortfall(
    *,
    observation: WholesaleAccountObservation,
    aggregate_available_wei: Decimal,
    limits: WholesaleFundingLimits,
) -> WholesaleFundingPlan:
    """Calculate bounded funding from a locked aggregate-account snapshot.

    ``aggregate_available_wei`` is the total from the serialized wholesale
    accounting transaction and includes this observation. The persistence
    layer must lock that aggregate before using this plan for a mint.
    """

    if aggregate_available_wei < observation.available_value_wei:
        raise WholesaleFundingPolicyError(
            "aggregate available value cannot be below the observed account"
        )
    shortfall = max(Decimal(0), limits.target_available_wei - observation.available_value_wei)
    projected_payee = observation.available_value_wei + shortfall
    projected_aggregate = aggregate_available_wei + shortfall
    if shortfall == 0:
        return WholesaleFundingPlan(
            target_available_wei=limits.target_available_wei,
            observed_available_wei=observation.available_value_wei,
            shortfall_wei=shortfall,
            projected_payee_available_wei=projected_payee,
            projected_aggregate_available_wei=projected_aggregate,
        )
    if shortfall > limits.max_single_funding_wei:
        raise WholesaleFundingPolicyError("account shortfall exceeds the single-funding limit")
    if projected_payee > limits.max_available_per_payee_wei:
        raise WholesaleFundingPolicyError("funding would exceed the per-payee limit")
    if projected_aggregate > limits.max_aggregate_available_wei:
        raise WholesaleFundingPolicyError("funding would exceed the aggregate limit")
    return WholesaleFundingPlan(
        target_available_wei=limits.target_available_wei,
        observed_available_wei=observation.available_value_wei,
        shortfall_wei=shortfall,
        projected_payee_available_wei=projected_payee,
        projected_aggregate_available_wei=projected_aggregate,
    )


def create_account_funding_request(
    *,
    route: SelectedRoute,
    observation: WholesaleAccountObservation,
    plan: WholesaleFundingPlan,
    mint_request_id: str,
) -> CreatePaymentRequest:
    """Build a funding-only daemon request from a trusted selected route."""

    if not route.features.wholesale_accounts:
        raise WholesaleFundingPolicyError("selected route does not support wholesale accounts")
    if observation.payee != route.eth_address.lower():
        raise WholesaleFundingPolicyError("account observation does not match the locked payee")
    if not mint_request_id:
        raise WholesaleFundingPolicyError("mint_request_id is required")
    try:
        recipient = bytes.fromhex(route.eth_address.removeprefix("0x"))
    except ValueError as exc:
        raise WholesaleFundingPolicyError("selected payee is not a hex address") from exc
    if len(recipient) != _ETH_ADDRESS_BYTES:
        raise WholesaleFundingPolicyError("selected payee must be a 20-byte address")
    accepted_price = AcceptedPrice(
        capability=route.capability,
        offering=route.offering,
        price_per_unit_wei=route.price_per_work_unit_wei,
        units_per_price=route.units_per_price,
        work_unit_name=route.work_unit,
        quote_ref=QuoteRef(
            quote_id=route.quote_id,
            quote_version=route.quote_version,
            constraint_fingerprint=route.constraint_fingerprint,
            route_fingerprint=route.route_fingerprint,
        ),
    )
    return CreatePaymentRequest(
        mint_request_id=mint_request_id,
        recipient=recipient,
        ticket_params_base_url=route.worker_url,
        accepted_price=accepted_price,
        # The legacy field must remain positive even for a zero-shortfall
        # replay. AccountFundingIntent, not this value, determines the mint.
        funding=FundingIntent(
            funded_value_wei=plan.target_available_wei,
            estimated_units=0,
            max_total_units=0,
        ),
        account_funding=AccountFundingIntent(
            target_available_wei=plan.target_available_wei,
            observed_available_wei=plan.observed_available_wei,
        ),
    )
