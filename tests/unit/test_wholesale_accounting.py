"""Schema and pricing invariants for separated customer/wholesale ledgers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from pydantic import ValidationError
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
    create_account_funding_request,
    plan_account_shortfall,
    record_minted_funding,
)
from livepeer_open_clearinghouse.domains.wholesale.types import WholesaleFundingLimits
from livepeer_open_clearinghouse.providers.broker_settlement import (
    WholesaleAccountObservation,
    WholesaleFundingResult,
)
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon import SelectedRoute


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
            "settlement_keys": [],
            "extra": {
                "job": {"schema": "openai-chat-completions/v1", "transports": ["unary"]},
                "features": {"wholesale_accounts": True},
            },
        }
    )


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
        plan=plan,
        limits=_limits(),
        mint_request_id="loc:account:durable",
        correlation_id="opaque-engagement",
        protocol_version="wholesale-account/1.0.0-draft",
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
            "credited_value_wei": Decimal(160),
            "available_value_wei": Decimal(100),
            "version": 4,
        }
    )
    acknowledged = await acknowledge_account_funding(
        wholesale_db,
        mint_request_id=claimed.mint_request_id,
        result=WholesaleFundingResult(
            payer=observation.payer,
            payee=observation.payee,
            credited_value_wei=60,
            available_value_wei=100,
            account_version=4,
            replayed=False,
        ),
        observation=after,
        acknowledged_at=datetime.now(UTC),
    )
    assert acknowledged.status == "acknowledged"
    assert acknowledged.account_version == 4

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
            plan=stale_plan,
            limits=_limits(max_aggregate_available_wei=150, max_single_funding_wei=100),
            mint_request_id="loc:account:blocked-by-global-cap",
            correlation_id=None,
            protocol_version="wholesale-account/1.0.0-draft",
        )
