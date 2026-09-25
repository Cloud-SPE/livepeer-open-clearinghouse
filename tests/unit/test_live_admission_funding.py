"""Live admission reserves must be covered independently of routine float policy."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from livepeer_open_clearinghouse.domains.billing import service as billing
from livepeer_open_clearinghouse.domains.sessions import service
from livepeer_open_clearinghouse.domains.sessions.repo import SpendAuthorizationGrant
from livepeer_open_clearinghouse.domains.wholesale.repo import (
    WholesaleExposureBudget,
    WholesaleFunding,
)
from livepeer_open_clearinghouse.errors import WholesaleFundingUnverified
from livepeer_open_clearinghouse.providers.broker_settlement import BrokerWholesaleAccountError
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon import MockRegistryClient
from tests.unit.test_open_session_service import (
    _clock,
    _route_for_protocol,
    _seed_user_key_and_balance,
    _settings,
    _WholesaleBroker,
)
from tests.unit.test_open_session_service import (
    db_session as db_session,  # noqa: PLC0414 — re-export fixture
)

AVAILABLE = 46_516_336_000_000
REQUIRED = 120_000_000_000_000


class AdmissionBroker(_WholesaleBroker):
    def __init__(self, *, lost_receipt=False, deplete=False):
        super().__init__()
        self.available_value_wei = Decimal(AVAILABLE)
        self.credited_value_wei = Decimal(AVAILABLE)
        self.funding_target = Decimal(REQUIRED)
        self.calls = []
        self.reads = 0
        self.lost_receipt = lost_receipt
        self.deplete = deplete

    async def get_wholesale_account(self, **kwargs):
        self.reads += 1
        result = await super().get_wholesale_account(**kwargs)
        if self.deplete and self.reads >= 3:
            return result.model_copy(
                update={"available_value_wei": Decimal(0), "reserved_value_wei": Decimal(REQUIRED)}
            )
        return result

    async def fund_wholesale_account(self, **kwargs):
        replayed = kwargs["payment_bytes"] in self.calls
        self.calls.append(kwargs["payment_bytes"])
        result = await super().fund_wholesale_account(**kwargs)
        if self.lost_receipt:
            self.lost_receipt = False
            raise BrokerWholesaleAccountError("receipt lost")
        return result.model_copy(update={"replayed": replayed})


async def admission_args(db, broker, *, single_limit=100_000_000_000_000):
    user, key = await _seed_user_key_and_balance(db, balance_wei=10**18)
    db.add(WholesaleExposureBudget(scope="global", projected_available_wei=Decimal(0)))
    await db.commit()
    route = _route_for_protocol("paid-session/v1").model_copy(
        update={"price_per_work_unit_wei": Decimal(10**12)}
    )
    settings = _settings().model_copy(
        update={
            "wholesale_chain_id": 42161,
            "wholesale_target_available_wei": 100_000_000_000_000,
            "wholesale_replenish_below_wei": 25_000_000_000_000,
            "wholesale_max_single_funding_wei": single_limit,
            "wholesale_max_available_per_payee_wei": 2_000_000_000_000_000,
            "wholesale_max_aggregate_available_wei": 3_000_000_000_000_000,
        }
    )
    registry = MockRegistryClient(routes=[route])
    prepared = await service.prepare_session(
        user_id=user,
        api_key_id=key,
        capability=route.capability,
        offering=route.offering,
        descriptor_schema="test-runtime/v1",
        route_binding=None,
        registry=registry,
        clock=_clock(),
        settings=settings,
    )
    return dict(
        user_id=user,
        api_key_id=key,
        capability=route.capability,
        offering=route.offering,
        descriptor_schema="test-runtime/v1",
        estimated_runway_units=60,
        max_total_units=120,
        gateway_session_id=prepared.gateway_session_id,
        preparation_token=prepared.preparation_token,
        route_binding=prepared.route_binding,
        sdk_identity=None,
        registry=registry,
        daemon=MockPaymentDaemonClient(),
        clock=_clock(),
        settings=settings,
        request_id="live-admission",
        workload_request_digest=b"\x55" * 32,
        caller_public_key=bytes.fromhex(
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        ),
        broker_wholesale=broker,
    )


@pytest.mark.unit
async def test_initial_admission_funds_production_shortfall_above_low_water(db_session):
    broker = AdmissionBroker()
    args = await admission_args(db_session, broker)
    response = await service.open_session(db_session, **args)
    funding = (await db_session.scalars(select(WholesaleFunding))).one()
    assert funding.requested_shortfall_wei == REQUIRED - AVAILABLE
    assert funding.status == "acknowledged"
    assert response.expected_value_wei == REQUIRED - AVAILABLE
    assert broker.available_value_wei == REQUIRED
    grant = (await db_session.scalars(select(SpendAuthorizationGrant))).one()
    assert grant.max_total_units == 120
    assert grant.max_debit_wei == REQUIRED


@pytest.mark.unit
async def test_lost_receipt_replays_original_mint_and_authorization(db_session):
    broker = AdmissionBroker(lost_receipt=True)
    args = await admission_args(db_session, broker)
    with pytest.raises(WholesaleFundingUnverified, match="receipt lost"):
        await service.open_session(db_session, **args)
    grant = (await db_session.scalars(select(SpendAuthorizationGrant))).one()
    original_bytes = grant.authorization_bytes
    response = await service.open_session(db_session, **args)
    assert response.session_id == grant.session_id
    assert grant.authorization_bytes == original_bytes
    assert len((await db_session.scalars(select(SpendAuthorizationGrant))).all()) == 1
    assert len((await db_session.scalars(select(WholesaleFunding))).all()) == 1
    assert len(broker.calls) == 2
    assert broker.calls[0] == broker.calls[1]
    balance = await billing.get_balance(db_session, user_id=args["user_id"])
    assert balance.amount_wei == 10**18 - REQUIRED


@pytest.mark.unit
@pytest.mark.parametrize("deplete", [False, True])
async def test_live_admission_failure_preserves_hold_and_scope(db_session, deplete):
    broker = AdmissionBroker(deplete=deplete)
    args = await admission_args(
        db_session, broker, single_limit=100_000_000_000_000 if deplete else 1
    )
    with pytest.raises(WholesaleFundingUnverified):
        await service.open_session(db_session, **args)
    assert len(broker.calls) == int(deplete)
    grant = (await db_session.scalars(select(SpendAuthorizationGrant))).one()
    assert grant.state == "issued"
    assert grant.max_debit_wei == REQUIRED
    balance = await billing.get_balance(db_session, user_id=args["user_id"])
    assert balance.amount_wei == 10**18 - REQUIRED


@pytest.mark.unit
@pytest.mark.parametrize("state", ["admitted", "issued", "canceled_unused"])
async def test_refill_reuses_only_verified_predecessor_reservation(db_session, monkeypatch, state):
    from livepeer_open_clearinghouse.providers.broker_settlement import SpendAuthorizationState

    broker = AdmissionBroker()
    args = await admission_args(db_session, broker)
    opened = await service.open_session(db_session, **args)
    predecessor = (await db_session.scalars(select(SpendAuthorizationGrant))).one()
    status = (
        await broker.get_spend_authorization(
            payer_eth_address=predecessor.payer_eth_address,
            authorization_id=predecessor.authorization_id,
        )
    ).model_copy(
        update={
            "state": SpendAuthorizationState(state),
            "billed_value_wei": Decimal(40_000_000_000_000),
            "reserved_value_wei": Decimal(80_000_000_000_000),
            "actual_units": 40,
        }
    )
    monkeypatch.setattr(broker, "get_spend_authorization", AsyncMock(return_value=status))
    broker.available_value_wei = Decimal(0)
    broker.reserved_value_wei = Decimal(80_000_000_000_000)
    broker.debited_value_wei = Decimal(40_000_000_000_000)
    broker.funding_target = Decimal(100_000_000_000_000)
    broker.version += 1
    kwargs = dict(
        session_id=opened.session_id,
        user_id=args["user_id"],
        api_key_id=args["api_key_id"],
        observed_consumed_units=None,
        daemon=args["daemon"],
        clock=args["clock"],
        settings=args["settings"],
        request_id="live-revision",
        max_total_units=180,
        workload_request_digest=b"\x66" * 32,
        broker_wholesale=broker,
    )
    if state != "admitted":
        with pytest.raises(WholesaleFundingUnverified, match="predecessor admission is unverified"):
            await service.refill_session(db_session, **kwargs)
        assert len(broker.calls) == 1
    else:
        revised = await service.refill_session(db_session, **kwargs)
        # Extra required credit is60e12; routine target100e12 covers it. Funding
        # the whole180e12 again would exceed the unchanged single-mint limit.
        assert revised.expected_value_wei == 100_000_000_000_000
        assert len(broker.calls) == 2
        grant = (
            await db_session.scalars(
                select(SpendAuthorizationGrant).where(SpendAuthorizationGrant.revision == 1)
            )
        ).one()
        assert (
            await service._session_reservation_requirement(db_session, grant=grant, broker=broker)
            == 60_000_000_000_000
        )
        assert grant.max_debit_wei == 180_000_000_000_000
