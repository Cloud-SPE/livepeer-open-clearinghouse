"""Schema and pricing invariants for separated customer/wholesale ledgers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.billing import service as billing_service
from livepeer_open_clearinghouse.domains.billing.repo import CreditLedger
from livepeer_open_clearinghouse.domains.billing.types import CustomerPricingSnapshot
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.wholesale.repo import (
    WholesaleAccount,
    WholesaleExposureBudget,
    WholesaleFunding,
)
from livepeer_open_clearinghouse.domains.wholesale.service import (
    WholesaleFundingPolicyError,
    acknowledge_account_funding,
    claim_account_funding,
    complete_account_funding,
    create_account_funding_request,
    plan_account_shortfall,
    plan_observed_account_shortfall,
    record_minted_funding,
)
from livepeer_open_clearinghouse.domains.wholesale.types import (
    COMPAT_SETTLEMENT_DOMAIN_ID,
    WholesaleFundingLimits,
    settlement_domain_id,
)
from livepeer_open_clearinghouse.providers.broker_settlement import (
    WholesaleAccountObservation,
    WholesaleFundingResult,
)
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon import SelectedRoute

_DOMAIN_A = settlement_domain_id("0x" + "aa" * 32)
_DOMAIN_B = settlement_domain_id("0x" + "bb" * 32)


@pytest_asyncio.fixture()
async def wholesale_db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(WholesaleAccount.__table__.create)
        await connection.run_sync(WholesaleExposureBudget.__table__.create)
        await connection.run_sync(WholesaleFunding.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(WholesaleExposureBudget(scope="global", projected_available_wei=Decimal(0)))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.unit
def test_wholesale_tables_have_no_customer_ownership_columns() -> None:
    forbidden = {"user_id", "api_key_id", "payment_id"}
    assert forbidden.isdisjoint(WholesaleAccount.__table__.columns.keys())
    assert forbidden.isdisjoint(WholesaleFunding.__table__.columns.keys())
    assert {
        "target_available_wei",
        "observed_available_wei",
        "route_snapshot",
        "payment_bytes",
    }.issubset(WholesaleFunding.__table__.columns.keys())
    assert {
        "chain_id",
        "payer_eth_address",
        "payee_eth_address",
        "settlement_domain_id",
        "denomination",
    }.issubset(WholesaleAccount.__table__.columns.keys())


@pytest.mark.unit
def test_customer_ledger_correlates_to_engagement_without_payment() -> None:
    assert "related_engagement_id" in CreditLedger.__table__.columns
    assert "customer_pricing" in PaymentSession.__table__.columns
    assert "customer_max_debit_wei" in PaymentSession.__table__.columns
    assert "authorization_id" in PaymentSession.__table__.columns


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "units", "wholesale", "expected"),
    [
        (
            CustomerPricingSnapshot(
                plan_id="pass-through",
                kind="wholesale_pass_through",
                work_unit="tokens",
            ),
            99,
            Decimal(123),
            Decimal(123),
        ),
        (
            CustomerPricingSnapshot(
                plan_id="plus-2.5pct",
                kind="cost_plus",
                work_unit="tokens",
                fee_basis_points=250,
            ),
            99,
            Decimal(101),
            Decimal(104),
        ),
        (
            CustomerPricingSnapshot(
                plan_id="retail-a",
                kind="unit_price",
                work_unit="tokens",
                price_per_unit_wei=Decimal(7),
                units_per_price=3,
            ),
            4,
            Decimal(500),
            Decimal(10),
        ),
    ],
)
def test_customer_charge_policy_is_independent_of_ticket_ev(
    policy: CustomerPricingSnapshot,
    units: int,
    wholesale: Decimal,
    expected: Decimal,
) -> None:
    assert (
        billing_service.calculate_customer_charge(
            policy, actual_units=units, wholesale_debit_wei=wholesale
        )
        == expected
    )


@pytest.mark.unit
def test_customer_pricing_policy_rejects_ambiguous_fields() -> None:
    with pytest.raises(ValidationError):
        CustomerPricingSnapshot(
            plan_id="bad",
            kind="wholesale_pass_through",
            work_unit="tokens",
            price_per_unit_wei=Decimal(1),
        )


def _account(*, available: int = 40) -> WholesaleAccountObservation:
    return WholesaleAccountObservation(
        payer="0x" + "aa" * 20,
        payee="0x" + "11" * 20,
        settlement_domain_id=str(_DOMAIN_A),
        chain_id=42161,
        denomination="wei",
        credited_value_wei=100,
        reserved_value_wei=10,
        debited_value_wei=50,
        available_value_wei=available,
        version=3,
        observed_at=datetime.now(UTC),
    )


def _limits(**updates: int) -> WholesaleFundingLimits:
    values = {
        "target_available_wei": 100,
        "replenish_below_wei": 75,
        "max_available_per_payee_wei": 120,
        "max_aggregate_available_wei": 500,
        "max_single_funding_wei": 75,
    }
    values.update(updates)
    return WholesaleFundingLimits.model_validate(values)


def _route() -> SelectedRoute:
    return SelectedRoute.model_validate(
        {
            "worker_url": "https://broker.example",
            "eth_address": "0x" + "11" * 20,
            "capability": "cap",
            "offering": "offer",
            "protocol": "paid-job/v1",
            "work_unit": "token",
            "price_per_work_unit_wei": "7",
            "units_per_price": 1000,
            "quote_id": "q-1",
            "quote_version": 1,
            "constraint_fingerprint": bytes.fromhex("22" * 32),
            "route_fingerprint": bytes.fromhex("33" * 32),
            "settlement_domain_id": str(_DOMAIN_A),
            "settlement_keys": [],
            "extra": {
                "job": {"schema": "openai-chat-completions/v1", "transports": ["unary"]},
            },
        }
    )


class _ReplayBroker:
    def __init__(self, observation: WholesaleAccountObservation) -> None:
        self.observation = observation
        self.fund_calls = 0

    async def fund_wholesale_account(self, **_: object) -> WholesaleFundingResult:
        self.fund_calls += 1
        return WholesaleFundingResult(
            payer=self.observation.payer,
            payee=self.observation.payee,
            settlement_domain_id=self.observation.settlement_domain_id,
            credited_value_wei=0,
            available_value_wei=self.observation.available_value_wei,
            account_version=self.observation.version,
            replayed=True,
        )

    async def get_wholesale_account(self, **_: object) -> WholesaleAccountObservation:
        return self.observation


class _NoMintDaemon:
    async def create_payment(self, *_: object, **__: object) -> object:
        raise AssertionError("a persisted mint must not call the daemon again")


def test_shortfall_plan_and_request_use_aggregate_account_not_customer_maximum() -> None:
    observation = _account(available=40)
    plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=240,
        limits=_limits(),
    )
    assert plan.shortfall_wei == 60
    assert plan.projected_aggregate_available_wei == 300
    request = create_account_funding_request(
        route=_route(),
        observation=observation,
        plan=plan,
        mint_request_id="loc:account:1",
    )
    assert request.funding.funded_value_wei == 100
    assert request.account_funding is not None
    assert request.account_funding.observed_available_wei == 40
    assert request.recipient == bytes.fromhex("11" * 20)


def test_shortfall_plan_mints_nothing_at_or_above_target() -> None:
    plan = plan_account_shortfall(
        observation=_account(available=125),
        aggregate_available_wei=300,
        limits=_limits(),
    )
    assert plan.shortfall_wei == 0


def test_shortfall_plan_preserves_residual_credit_above_low_water_mark() -> None:
    plan = plan_account_shortfall(
        observation=_account(available=75),
        aggregate_available_wei=250,
        limits=_limits(),
    )
    assert plan.shortfall_wei == 0
    assert plan.projected_payee_available_wei == 75


@pytest.mark.parametrize(
    ("aggregate", "updates", "message"),
    [
        (40, {"max_single_funding_wei": 50}, "single-funding"),
        (490, {}, "aggregate"),
    ],
)
def test_shortfall_plan_fails_closed_at_operator_limits(
    aggregate: int, updates: dict[str, int], message: str
) -> None:
    with pytest.raises(WholesaleFundingPolicyError, match=message):
        plan_account_shortfall(
            observation=_account(available=40),
            aggregate_available_wei=Decimal(aggregate),
            limits=_limits(**updates),
        )


@pytest.mark.asyncio
async def test_observed_plan_repairs_stale_projected_aggregate_from_accounts(
    wholesale_db: AsyncSession,
) -> None:
    budget = await wholesale_db.get(WholesaleExposureBudget, "global")
    assert budget is not None
    budget.projected_available_wei = Decimal(200)
    wholesale_db.add(
        WholesaleAccount(
            chain_id=42161,
            payer_eth_address="0x" + "aa" * 20,
            payee_eth_address="0x" + "11" * 20,
            settlement_domain_id=_DOMAIN_A,
            denomination="wei",
            protocol_version="wholesale-account/1.1.0-draft",
            broker_url="https://broker.example",
            credited_value_wei=Decimal(100),
            reserved_value_wei=Decimal(10),
            debited_value_wei=Decimal(50),
            available_value_wei=Decimal(40),
            remote_version=3,
            observed_at=datetime.now(UTC),
        )
    )
    await wholesale_db.commit()

    plan = await plan_observed_account_shortfall(
        wholesale_db,
        observation=_account(available=60).model_copy(update={"version": 4}),
        settlement_domain_id=_DOMAIN_A,
        limits=_limits(),
    )
    assert plan.shortfall_wei == 40
    assert plan.projected_aggregate_available_wei == 100


@pytest.mark.asyncio
async def test_compatibility_account_is_audit_only_for_protocol_4_exposure(
    wholesale_db: AsyncSession,
) -> None:
    budget = await wholesale_db.get(WholesaleExposureBudget, "global")
    assert budget is not None
    budget.projected_available_wei = Decimal(870)
    wholesale_db.add(
        WholesaleAccount(
            chain_id=42161,
            payer_eth_address="0x" + "aa" * 20,
            payee_eth_address="0x" + "11" * 20,
            settlement_domain_id=COMPAT_SETTLEMENT_DOMAIN_ID,
            denomination="wei",
            protocol_version="wholesale-account/1.1.0-draft",
            broker_url="https://retired-broker.example",
            credited_value_wei=Decimal(877),
            reserved_value_wei=Decimal(0),
            debited_value_wei=Decimal(7),
            available_value_wei=Decimal(870),
            remote_version=32,
            observed_at=datetime.now(UTC),
        )
    )
    await wholesale_db.commit()

    observation = _account(available=0).model_copy(
        update={"settlement_domain_id": str(_DOMAIN_B), "version": 0}
    )
    limits = _limits(
        max_available_per_payee_wei=150,
        max_aggregate_available_wei=150,
        max_single_funding_wei=100,
    )
    plan = await plan_observed_account_shortfall(
        wholesale_db,
        observation=observation,
        settlement_domain_id=_DOMAIN_B,
        limits=limits,
    )
    assert plan.shortfall_wei == 100
    assert plan.projected_payee_available_wei == 100
    assert plan.projected_aggregate_available_wei == 100

    funding = await claim_account_funding(
        wholesale_db,
        route=_route().model_copy(update={"settlement_domain_id": str(_DOMAIN_B)}),
        observation=observation,
        settlement_domain_id=_DOMAIN_B,
        plan=plan,
        limits=limits,
        mint_request_id="loc:account:after-compat-cutover",
        correlation_id=None,
        protocol_version="wholesale-account/2.0.0-draft",
    )
    assert funding is not None
    assert budget.projected_available_wei == Decimal(100)


@pytest.mark.asyncio
async def test_same_payee_has_independent_domain_balances_and_versions(
    wholesale_db: AsyncSession,
) -> None:
    budget = await wholesale_db.get(WholesaleExposureBudget, "global")
    assert budget is not None
    budget.projected_available_wei = Decimal(870)
    wholesale_db.add(
        WholesaleAccount(
            chain_id=42161,
            payer_eth_address="0x" + "aa" * 20,
            payee_eth_address="0x" + "11" * 20,
            settlement_domain_id=_DOMAIN_A,
            denomination="wei",
            protocol_version="wholesale-account/1.1.0-draft",
            broker_url="https://broker-a.example",
            credited_value_wei=Decimal(877),
            reserved_value_wei=Decimal(0),
            debited_value_wei=Decimal(7),
            available_value_wei=Decimal(870),
            remote_version=34,
            observed_at=datetime.now(UTC),
        )
    )
    await wholesale_db.commit()

    domain_b_observation = _account(available=0).model_copy(
        update={
            "settlement_domain_id": str(_DOMAIN_B),
            "credited_value_wei": Decimal(0),
            "reserved_value_wei": Decimal(0),
            "debited_value_wei": Decimal(0),
            "version": 0,
        }
    )
    limits = _limits(
        max_available_per_payee_wei=1000,
        max_aggregate_available_wei=1000,
        max_single_funding_wei=100,
    )
    plan = await plan_observed_account_shortfall(
        wholesale_db,
        observation=domain_b_observation,
        settlement_domain_id=_DOMAIN_B,
        limits=limits,
    )
    assert plan.shortfall_wei == 100
    assert plan.projected_payee_available_wei == 970
    assert plan.projected_aggregate_available_wei == 970
    with pytest.raises(WholesaleFundingPolicyError, match="per-payee"):
        await plan_observed_account_shortfall(
            wholesale_db,
            observation=domain_b_observation,
            settlement_domain_id=_DOMAIN_B,
            limits=limits.model_copy(update={"max_available_per_payee_wei": Decimal(900)}),
        )

    funding = await claim_account_funding(
        wholesale_db,
        route=_route().model_copy(
            update={
                "worker_url": "https://broker-b.example",
                "settlement_domain_id": str(_DOMAIN_B),
            }
        ),
        observation=domain_b_observation,
        settlement_domain_id=_DOMAIN_B,
        plan=plan,
        limits=limits,
        mint_request_id="loc:account:domain-b",
        correlation_id=None,
        protocol_version="wholesale-account/1.1.0-draft",
    )
    assert funding is not None
    accounts = list((await wholesale_db.scalars(select(WholesaleAccount))).all())
    assert {(account.settlement_domain_id, account.remote_version) for account in accounts} == {
        (_DOMAIN_A, 34),
        (_DOMAIN_B, 0),
    }

    with pytest.raises(WholesaleFundingPolicyError, match="per-payee"):
        await plan_observed_account_shortfall(
            wholesale_db,
            observation=domain_b_observation.model_copy(
                update={"settlement_domain_id": "0x" + "cc" * 32}
            ),
            settlement_domain_id=settlement_domain_id("0x" + "cc" * 32),
            limits=limits,
        )

    with pytest.raises(WholesaleFundingPolicyError, match="settlement domain"):
        await claim_account_funding(
            wholesale_db,
            route=_route().model_copy(update={"worker_url": "https://broker-b.example"}),
            observation=domain_b_observation,
            settlement_domain_id=_DOMAIN_A,
            plan=plan,
            limits=limits,
            mint_request_id="loc:account:domain-b",
            correlation_id=None,
            protocol_version="wholesale-account/1.1.0-draft",
        )


@pytest.mark.asyncio
async def test_funding_claim_mint_and_ack_are_durable(
    wholesale_db: AsyncSession,
) -> None:
    route = _route()
    observation = _account(available=40)
    plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=40,
        limits=_limits(),
    )
    claimed = await claim_account_funding(
        wholesale_db,
        route=route,
        observation=observation,
        settlement_domain_id=_DOMAIN_A,
        plan=plan,
        limits=_limits(),
        mint_request_id="loc:account:durable",
        correlation_id="opaque-engagement",
        protocol_version="wholesale-account/1.1.0-draft",
    )
    assert claimed is not None
    assert claimed.status == "claimed"
    request = create_account_funding_request(
        route=route,
        observation=observation,
        plan=plan,
        mint_request_id=claimed.mint_request_id,
    )
    response = await MockPaymentDaemonClient().create_payment(request)
    minted = await record_minted_funding(
        wholesale_db, mint_request_id=claimed.mint_request_id, response=response
    )
    assert minted.status == "minted"
    assert minted.payment_bytes == response.payment_bytes
    after = observation.model_copy(
        update={
            "credited_value_wei": Decimal(175),
            "available_value_wei": Decimal(115),
            "version": 4,
        }
    )
    acknowledged = await acknowledge_account_funding(
        wholesale_db,
        mint_request_id=claimed.mint_request_id,
        result=WholesaleFundingResult(
            payer=observation.payer,
            payee=observation.payee,
            settlement_domain_id=observation.settlement_domain_id,
            credited_value_wei=75,
            available_value_wei=115,
            account_version=4,
            replayed=False,
        ),
        observation=after,
        settlement_domain_id=_DOMAIN_A,
        acknowledged_at=datetime.now(UTC),
    )
    assert acknowledged.status == "acknowledged"
    assert acknowledged.account_version == 4
    assert acknowledged.credited_value_wei == 75

    second_route = route.model_copy(update={"eth_address": "0x" + "22" * 20})
    second_observation = _account(available=0).model_copy(update={"payee": "0x" + "22" * 20})
    stale_plan = plan_account_shortfall(
        observation=second_observation,
        aggregate_available_wei=0,
        limits=_limits(max_aggregate_available_wei=150, max_single_funding_wei=100),
    )
    with pytest.raises(WholesaleFundingPolicyError, match="aggregate"):
        await claim_account_funding(
            wholesale_db,
            route=second_route,
            observation=second_observation,
            settlement_domain_id=_DOMAIN_A,
            plan=stale_plan,
            limits=_limits(max_aggregate_available_wei=150, max_single_funding_wei=100),
            mint_request_id="loc:account:blocked-by-global-cap",
            correlation_id=None,
            protocol_version="wholesale-account/1.1.0-draft",
        )


@pytest.mark.asyncio
async def test_minted_funding_replays_persisted_bytes_without_reminting(
    wholesale_db: AsyncSession,
) -> None:
    route = _route()
    observation = _account(available=40)
    plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=40,
        limits=_limits(),
    )
    funding = await claim_account_funding(
        wholesale_db,
        route=route,
        observation=observation,
        settlement_domain_id=_DOMAIN_A,
        plan=plan,
        limits=_limits(),
        mint_request_id="loc:account:recover-minted",
        correlation_id="engagement-recovery",
        protocol_version="wholesale-account/1.1.0-draft",
    )
    assert funding is not None
    request = create_account_funding_request(
        route=route,
        observation=observation,
        plan=plan,
        mint_request_id=funding.mint_request_id,
    )
    response = await MockPaymentDaemonClient().create_payment(request)
    funding = await record_minted_funding(
        wholesale_db,
        mint_request_id=funding.mint_request_id,
        response=response,
    )
    after = observation.model_copy(
        update={
            "credited_value_wei": Decimal(160),
            "available_value_wei": Decimal(100),
            "version": 4,
        }
    )
    broker = _ReplayBroker(after)

    await complete_account_funding(
        wholesale_db,
        funding=funding,
        route=route,
        observation=after,
        settlement_domain_id=_DOMAIN_A,
        plan=plan_account_shortfall(
            observation=after,
            aggregate_available_wei=100,
            limits=_limits(),
        ),
        payer_eth_address=observation.payer,
        chain_id=observation.chain_id,
        broker=broker,
        daemon=_NoMintDaemon(),  # type: ignore[arg-type]
        acknowledged_at=datetime.now(UTC),
    )
    assert broker.fund_calls == 1
    recovered = await wholesale_db.get(WholesaleFunding, funding.id)
    assert recovered is not None
    assert recovered.status == "acknowledged"
    assert recovered.credited_value_wei == 60


def test_admission_shortfall_matches_production_rejection_without_raising_caps():
    from livepeer_open_clearinghouse.domains.wholesale.service import admission_funding_limits

    limits = WholesaleFundingLimits(
        target_available_wei=10**14,
        replenish_below_wei=25 * 10**12,
        max_available_per_payee_wei=2 * 10**15,
        max_aggregate_available_wei=3 * 10**15,
        max_single_funding_wei=10**14,
    )
    effective = admission_funding_limits(limits, required_reservation_wei=Decimal(1629000000000000))
    observation = _account(available=565108281000000)
    assert effective.max_single_funding_wei == limits.max_single_funding_wei
    with pytest.raises(WholesaleFundingPolicyError, match="single-funding limit"):
        plan_account_shortfall(
            observation=observation,
            aggregate_available_wei=observation.available_value_wei,
            limits=effective,
        )
    # This calculation shows the required policy change; it does not change production limits.
    approved = effective.model_copy(update={"max_single_funding_wei": Decimal(2 * 10**15)})
    plan = plan_account_shortfall(
        observation=observation,
        aggregate_available_wei=observation.available_value_wei,
        limits=approved,
    )
    assert plan.shortfall_wei == 1063891719000000


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payer", "0x" + "bb" * 20),
        ("payee", "0x" + "cc" * 20),
        ("chain_id", 1),
        ("settlement_domain_id", str(_DOMAIN_B)),
        ("denomination", "other"),
    ],
)
async def test_job_admission_readiness_rejects_changed_account_identity(field, value):
    from unittest.mock import AsyncMock

    from livepeer_open_clearinghouse.domains.wholesale.service import (
        verify_admission_funding_readiness,
    )

    broker = _ReplayBroker(_account())
    broker.get_wholesale_account = AsyncMock(
        return_value=_account().model_copy(update={field: value})
    )
    with pytest.raises(WholesaleFundingPolicyError, match="changed account identity"):
        await verify_admission_funding_readiness(
            broker=broker,
            route=_route(),
            payer_eth_address="0x" + "aa" * 20,
            chain_id=42161,
            required_reservation_wei=Decimal(1),
        )
