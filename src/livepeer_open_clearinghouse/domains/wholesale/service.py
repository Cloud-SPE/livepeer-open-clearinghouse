"""Planning and request construction for aggregate wholesale funding."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from livepeer_open_clearinghouse.domains.wholesale.repo import (
    WholesaleAccount,
    WholesaleExposureBudget,
    WholesaleFunding,
)
from livepeer_open_clearinghouse.domains.wholesale.types import (
    COMPAT_SETTLEMENT_DOMAIN_ID,
    SettlementDomainId,
    WholesaleFundingLimits,
    WholesaleFundingPlan,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    BrokerWholesaleAccountClient,
    WholesaleAccountObservation,
    WholesaleFundingResult,
)
from livepeer_open_clearinghouse.providers.payment_daemon import (
    AcceptedPrice,
    AccountFundingIntent,
    CreatePaymentRequest,
    CreatePaymentResponse,
    FundingIntent,
    PaymentDaemonClient,
    QuoteRef,
    validate_funding_response,
)
from livepeer_open_clearinghouse.providers.registry_daemon import SelectedRoute

_ETH_ADDRESS_BYTES = 20
WHOLESALE_ACCOUNT_PROTOCOL_VERSION = "wholesale-account/2.0.0-draft"


class WholesaleFundingPolicyError(ValueError):
    """Funding would exceed an operator-controlled exposure boundary."""


def admission_funding_limits(
    limits: WholesaleFundingLimits, *, required_reservation_wei: Decimal
) -> WholesaleFundingLimits:
    """Raise the refill floor for one admission without raising exposure limits.

    A healthy routine float can still be too small for an individual reservation.
    The maximum signed debit, not the workload estimate, is reserved by paid jobs.
    """

    if not required_reservation_wei.is_finite() or required_reservation_wei < 0:
        raise WholesaleFundingPolicyError("invalid required reservation")
    target = max(limits.target_available_wei, required_reservation_wei)
    if target > limits.max_available_per_payee_wei:
        raise WholesaleFundingPolicyError("job reservation exceeds the per-payee funding limit")
    if target > limits.max_aggregate_available_wei:
        raise WholesaleFundingPolicyError("job reservation exceeds the aggregate funding limit")
    return limits.model_copy(
        update={
            "target_available_wei": target,
            "replenish_below_wei": max(limits.replenish_below_wei, required_reservation_wei),
        }
    )


async def verify_job_funding_readiness(
    *,
    broker: BrokerWholesaleAccountClient,
    route: SelectedRoute,
    payer_eth_address: str,
    chain_id: int,
    required_reservation_wei: Decimal,
) -> None:
    """Check receiver-acknowledged free credit before disclosing a job grant.

    This is a readiness check, not an admission reservation. Only the receiver can
    atomically reserve funds; another client may still consume credit afterwards.
    """

    observed = await broker.get_wholesale_account(
        broker_url=route.worker_url,
        payer_eth_address=payer_eth_address,
        payee_eth_address=route.eth_address,
        chain_id=chain_id,
        settlement_domain_id=route.settlement_domain_id,
    )
    if (
        observed.payer != payer_eth_address
        or observed.payee != route.eth_address
        or observed.chain_id != chain_id
        or observed.denomination != "wei"
        or observed.settlement_domain_id != route.settlement_domain_id
    ):
        raise WholesaleFundingPolicyError("admission readiness changed account identity")
    if observed.available_value_wei < required_reservation_wei:
        raise WholesaleFundingPolicyError(
            "receiver available credit is below the job reservation after funding"
        )


async def claim_account_funding(
    db: AsyncSession,
    *,
    route: SelectedRoute,
    observation: WholesaleAccountObservation,
    settlement_domain_id: SettlementDomainId,
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

    _require_settlement_domain_id(settlement_domain_id)
    _require_observation_domain(observation, settlement_domain_id)
    if db.new or db.dirty or db.deleted:
        raise WholesaleFundingPolicyError("funding claim requires a clean database session")
    existing = await db.scalar(
        select(WholesaleFunding).where(WholesaleFunding.mint_request_id == mint_request_id)
    )
    if existing is not None:
        existing_account = await db.get(WholesaleAccount, existing.account_id)
        if existing_account is None or not _account_matches(
            existing_account,
            observation=observation,
            settlement_domain_id=settlement_domain_id,
        ):
            raise WholesaleFundingPolicyError("mint_request_id replay changed account identity")
        recorded_identity = (
            existing.target_available_wei,
            existing.route_snapshot,
            existing.correlation_id,
        )
        requested_identity = (
            plan.target_available_wei,
            route.snapshot(),
            correlation_id,
        )
        if recorded_identity != requested_identity:
            raise WholesaleFundingPolicyError("mint_request_id replay changed funding scope")
        if existing.status == "claimed" and (
            existing.observed_available_wei,
            existing.requested_shortfall_wei,
        ) != (plan.observed_available_wei, plan.shortfall_wei):
            raise WholesaleFundingPolicyError("unminted funding replay changed account state")
        await db.commit()
        return existing
    budget = await db.scalar(
        select(WholesaleExposureBudget)
        .where(WholesaleExposureBudget.scope == "global")
        .with_for_update()
    )
    if budget is None:
        raise WholesaleFundingPolicyError("wholesale exposure budget is not initialized")
    active_accounts = await _load_active_accounts(db, lock=True)
    payee_accounts = _matching_payee_accounts(active_accounts, observation)
    account = next(
        (
            candidate
            for candidate in payee_accounts
            if candidate.settlement_domain_id == settlement_domain_id
        ),
        None,
    )
    synchronized_aggregate, synchronized_payee = await _synchronized_exposure(
        db,
        active_accounts=active_accounts,
        payee_accounts=payee_accounts,
        account=account,
        observation=observation,
    )
    locked_plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=synchronized_aggregate,
        payee_available_wei=synchronized_payee,
        limits=limits,
    )
    if locked_plan != plan:
        raise WholesaleFundingPolicyError("funding plan changed while acquiring exposure lock")
    if account is None:
        account = WholesaleAccount(
            chain_id=observation.chain_id,
            payer_eth_address=observation.payer,
            payee_eth_address=observation.payee,
            settlement_domain_id=settlement_domain_id,
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


async def complete_account_funding(
    db: AsyncSession,
    *,
    funding: WholesaleFunding | None,
    route: SelectedRoute,
    observation: WholesaleAccountObservation,
    settlement_domain_id: SettlementDomainId,
    plan: WholesaleFundingPlan,
    payer_eth_address: str,
    chain_id: int,
    broker: BrokerWholesaleAccountClient,
    daemon: PaymentDaemonClient,
    acknowledged_at: datetime,
) -> None:
    """Mint or replay a claimed shortfall and durably converge broker state."""

    if funding is None or funding.status == "acknowledged":
        return
    account = await db.get(WholesaleAccount, funding.account_id)
    if account is None or not _account_matches(
        account,
        observation=observation,
        settlement_domain_id=settlement_domain_id,
    ):
        raise WholesaleFundingPolicyError("funding completion changed account identity")
    payment_bytes = funding.payment_bytes
    if funding.status == "claimed":
        mint_request = create_account_funding_request(
            route=route,
            observation=observation,
            plan=plan,
            mint_request_id=funding.mint_request_id,
        )
        mint_response = validate_funding_response(
            mint_request, await daemon.create_payment(mint_request)
        )
        if "0x" + mint_response.sender.hex() != payer_eth_address:
            raise WholesaleFundingPolicyError("funding payer differs from authorization payer")
        funding = await record_minted_funding(
            db, mint_request_id=funding.mint_request_id, response=mint_response
        )
        payment_bytes = funding.payment_bytes
    if payment_bytes is None:
        raise WholesaleFundingPolicyError("minted funding has no replayable payment bytes")
    result = await broker.fund_wholesale_account(
        broker_url=route.worker_url,
        capability=route.capability,
        offering=route.offering,
        payment_bytes=payment_bytes,
        payer_eth_address=payer_eth_address,
        payee_eth_address=route.eth_address,
        settlement_domain_id=str(settlement_domain_id),
    )
    after = await broker.get_wholesale_account(
        broker_url=route.worker_url,
        payer_eth_address=payer_eth_address,
        payee_eth_address=route.eth_address,
        chain_id=chain_id,
        settlement_domain_id=str(settlement_domain_id),
    )
    await acknowledge_account_funding(
        db,
        mint_request_id=funding.mint_request_id,
        result=result,
        observation=after,
        settlement_domain_id=settlement_domain_id,
        acknowledged_at=acknowledged_at,
    )


async def plan_observed_account_shortfall(
    db: AsyncSession,
    *,
    observation: WholesaleAccountObservation,
    settlement_domain_id: SettlementDomainId,
    limits: WholesaleFundingLimits,
) -> WholesaleFundingPlan:
    """Plan against LOC's current aggregate view before taking the claim lock.

    ``claim_account_funding`` repeats this calculation under row locks and
    rejects a stale plan.  This first pass therefore supplies the exact input
    for the uncontended case without weakening concurrent cap enforcement.
    """

    _require_settlement_domain_id(settlement_domain_id)
    _require_observation_domain(observation, settlement_domain_id)
    budget = await db.get(WholesaleExposureBudget, "global")
    if budget is None:
        raise WholesaleFundingPolicyError("wholesale exposure budget is not initialized")
    active_accounts = await _load_active_accounts(db, lock=False)
    payee_accounts = _matching_payee_accounts(active_accounts, observation)
    account = next(
        (
            candidate
            for candidate in payee_accounts
            if candidate.settlement_domain_id == settlement_domain_id
        ),
        None,
    )
    prior_available = Decimal(0) if account is None else account.available_value_wei
    prior_payee_available = sum(
        (candidate.available_value_wei for candidate in payee_accounts), Decimal(0)
    ) + await _pending_account_funding(db, payee_accounts)
    prior_aggregate_available = sum(
        (candidate.available_value_wei for candidate in active_accounts), Decimal(0)
    ) + await _pending_account_funding(db, active_accounts)
    synchronized_aggregate = (
        prior_aggregate_available - prior_available + observation.available_value_wei
    )
    synchronized_payee = prior_payee_available - prior_available + observation.available_value_wei
    return plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=synchronized_aggregate,
        payee_available_wei=synchronized_payee,
        limits=limits,
    )


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
    settlement_domain_id: SettlementDomainId,
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
    _require_settlement_domain_id(settlement_domain_id)
    _require_observation_domain(observation, settlement_domain_id)
    if (
        result.payer != account.payer_eth_address
        or result.payee != account.payee_eth_address
        or result.settlement_domain_id != settlement_domain_id
        or not _account_matches(
            account,
            observation=observation,
            settlement_domain_id=settlement_domain_id,
        )
    ):
        raise WholesaleFundingPolicyError("broker acknowledgement changed account identity")
    if result.account_version != observation.version:
        raise WholesaleFundingPolicyError("broker acknowledgement and observation disagree")
    if result.available_value_wei != observation.available_value_wei:
        raise WholesaleFundingPolicyError("broker acknowledgement and observation disagree")
    if observation.version < account.remote_version:
        raise WholesaleFundingPolicyError("broker account observation moved backwards")
    acknowledged_credit = _acknowledged_credit(
        result=result,
        observation=observation,
        account=account,
        funding=funding,
    )
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
    funding.credited_value_wei = acknowledged_credit
    funding.account_version = result.account_version
    funding.status = "acknowledged"
    funding.acknowledged_at = acknowledged_at
    await db.commit()
    return funding


def _acknowledged_credit(
    *,
    result: WholesaleFundingResult,
    observation: WholesaleAccountObservation,
    account: WholesaleAccount,
    funding: WholesaleFunding,
) -> Decimal:
    """Prove a first transfer or recover it from the monotonic account total."""

    credited_delta = observation.credited_value_wei - account.credited_value_wei
    if result.replayed:
        if result.credited_value_wei != 0:
            raise WholesaleFundingPolicyError("broker funding replay transferred new credit")
        if (
            observation.version <= account.remote_version
            or credited_delta < funding.requested_shortfall_wei
        ):
            raise WholesaleFundingPolicyError(
                "broker funding replay is not proven by durable account credit"
            )
        return credited_delta
    if result.credited_value_wei < funding.requested_shortfall_wei:
        raise WholesaleFundingPolicyError("broker under-credited account funding")
    if credited_delta < result.credited_value_wei:
        raise WholesaleFundingPolicyError(
            "broker funding credit is not reflected in the durable account"
        )
    return result.credited_value_wei


def _require_settlement_domain_id(value: SettlementDomainId) -> None:
    if not value:
        raise WholesaleFundingPolicyError("settlement_domain_id is required")


def _require_observation_domain(
    observation: WholesaleAccountObservation, value: SettlementDomainId
) -> None:
    if observation.settlement_domain_id != value:
        raise WholesaleFundingPolicyError("broker observation changed settlement domain")


async def _pending_account_funding(db: AsyncSession, accounts: list[WholesaleAccount]) -> Decimal:
    """Conservatively include deposits claimed but not yet observed as credit."""

    if not accounts:
        return Decimal(0)
    value = await db.scalar(
        select(func.sum(WholesaleFunding.requested_shortfall_wei)).where(
            WholesaleFunding.account_id.in_(account.id for account in accounts),
            WholesaleFunding.status != "acknowledged",
        )
    )
    return Decimal(0) if value is None else value


async def _load_active_accounts(db: AsyncSession, *, lock: bool) -> list[WholesaleAccount]:
    """Load Protocol 4 accounts while retaining compatibility rows for audit."""

    statement = select(WholesaleAccount).where(
        WholesaleAccount.settlement_domain_id != COMPAT_SETTLEMENT_DOMAIN_ID
    )
    if lock:
        statement = statement.with_for_update()
    return list(await db.scalars(statement))


def _matching_payee_accounts(
    accounts: list[WholesaleAccount], observation: WholesaleAccountObservation
) -> list[WholesaleAccount]:
    return [
        candidate
        for candidate in accounts
        if candidate.chain_id == observation.chain_id
        and candidate.payer_eth_address == observation.payer
        and candidate.payee_eth_address == observation.payee
        and candidate.denomination == observation.denomination
    ]


async def _synchronized_exposure(
    db: AsyncSession,
    *,
    active_accounts: list[WholesaleAccount],
    payee_accounts: list[WholesaleAccount],
    account: WholesaleAccount | None,
    observation: WholesaleAccountObservation,
) -> tuple[Decimal, Decimal]:
    """Replace one stale local observation in current Protocol 4 exposure."""

    prior_available = Decimal(0) if account is None else account.available_value_wei
    prior_payee = sum(
        (candidate.available_value_wei for candidate in payee_accounts), Decimal(0)
    ) + await _pending_account_funding(db, payee_accounts)
    prior_aggregate = sum(
        (candidate.available_value_wei for candidate in active_accounts), Decimal(0)
    ) + await _pending_account_funding(db, active_accounts)
    replacement = observation.available_value_wei - prior_available
    return prior_aggregate + replacement, prior_payee + replacement


def _account_matches(
    account: WholesaleAccount,
    *,
    observation: WholesaleAccountObservation,
    settlement_domain_id: SettlementDomainId,
) -> bool:
    """Compare all stable coordinates without interpreting the opaque domain ID."""

    return (
        account.chain_id == observation.chain_id
        and account.payer_eth_address == observation.payer
        and account.payee_eth_address == observation.payee
        and observation.settlement_domain_id == settlement_domain_id
        and account.settlement_domain_id == settlement_domain_id
        and account.denomination == observation.denomination
    )


def plan_account_shortfall(
    *,
    observation: WholesaleAccountObservation,
    aggregate_available_wei: Decimal,
    payee_available_wei: Decimal | None = None,
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
    if payee_available_wei is None:
        payee_available_wei = observation.available_value_wei
    if payee_available_wei < observation.available_value_wei:
        raise WholesaleFundingPolicyError(
            "payee available value cannot be below the observed account"
        )
    should_replenish = observation.available_value_wei < limits.replenish_below_wei
    shortfall = (
        max(Decimal(0), limits.target_available_wei - observation.available_value_wei)
        if should_replenish
        else Decimal(0)
    )
    projected_payee = payee_available_wei + shortfall
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

    if observation.payee != route.eth_address.lower():
        raise WholesaleFundingPolicyError("account observation does not match the locked payee")
    if observation.settlement_domain_id != route.settlement_domain_id:
        raise WholesaleFundingPolicyError(
            "account observation does not match the locked settlement domain"
        )
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
            settlement_domain_id=route.settlement_domain_id,
        ),
    )
