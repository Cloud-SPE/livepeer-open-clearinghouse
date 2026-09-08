"""Usage views over settled payment sessions: jobs, summary, overview, attention."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.admin.types import AdminUserView, DepositSnapshotView
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.billing.types import BalanceView, LedgerEntryView
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.payments.types import PaymentView
from livepeer_open_clearinghouse.domains.sessions import repo as _sessions  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.telemetry import repo as _telemetry  # noqa: F401
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
from livepeer_open_clearinghouse.domains.usage import service as usage
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.settings import Settings

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()


def _clock() -> FrozenClock:
    return FrozenClock(NOW)


def _settings(cap_wei: int = 0) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        default_spend_period_seconds=86400,
        default_spend_period_cap_wei=cap_wei,
    )


async def _user(db: AsyncSession, *, balance_wei: int, label: str = "prod") -> tuple[User, ApiKey]:
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email=f"{uid.hex[:8]}@example.com",
        email_verified_at=NOW,
        password_hash="x",
    )
    key = ApiKey(
        id=uuid.uuid4(),
        user_id=uid,
        prefix=f"loc_test_{uid.hex[:8]}",
        hash="h",
        label=label,
    )
    db.add_all([user, key])
    await db.flush()
    db.add(CreditBalance(user_id=uid, amount_wei=Decimal(balance_wei)))
    await db.flush()
    return user, key


def _row(
    user: User,
    key: ApiKey,
    *,
    capability: str = "openai:chat-completions",
    offering: str = "qwen3.6-27b",
    protocol: str = "paid-job/v1",
    work_unit: str = "tokens",
    state: str = "closed",
    funded: int = 830_000_000_000,
    billed: int | None = 24_070_000_000,
    units: int | None = 29,
    opened_at: datetime = NOW - timedelta(minutes=5),
    closed_at: datetime | None = NOW - timedelta(minutes=4),
    outcome: str | None = "OVERFUNDED",
) -> PaymentSession:
    return PaymentSession(
        id=uuid.uuid4(),
        user_id=user.id,
        api_key_id=key.id,
        work_id=uuid.uuid4().hex,
        capability=capability,
        offering=offering,
        protocol=protocol,
        route_snapshot={"work_unit": work_unit},
        state=state,
        estimated_units=300,
        max_total_units=1000,
        funded_value_wei=Decimal(funded),
        billed_value_wei=Decimal(billed) if billed is not None else None,
        actual_units=units,
        outcome=outcome,
        opened_at=opened_at.replace(tzinfo=None),
        closed_at=closed_at.replace(tzinfo=None) if closed_at is not None else None,
    )


async def _seed_mixed(db: AsyncSession) -> tuple[User, ApiKey, list[PaymentSession]]:
    user, key = await _user(db, balance_wei=10**16)
    rows = [
        _row(user, key),
        _row(
            user,
            key,
            capability="video:transcode.vod",
            offering="vod-default",
            work_unit="video-frame-megapixel",
            funded=3_258_000_000_000,
            billed=200_367_000_000,
            units=123,
            opened_at=NOW - timedelta(days=2),
            closed_at=NOW - timedelta(days=2) + timedelta(seconds=9),
        ),
        # Open and fresh: held, not spent.
        _row(
            user,
            key,
            state="open",
            billed=None,
            units=None,
            closed_at=None,
            outcome=None,
            opened_at=NOW - timedelta(minutes=2),
        ),
        # Open and stale: unresolved.
        _row(
            user,
            key,
            protocol="paid-session/v1",
            capability="video:transcode.live",
            offering="gateway-ingest",
            work_unit="output_seconds",
            state="open",
            funded=600_000_000_000_000,
            billed=None,
            units=None,
            closed_at=None,
            outcome=None,
            opened_at=NOW - timedelta(hours=1),
        ),
    ]
    db.add_all(rows)
    await db.flush()
    return user, key, rows


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_jobs_reports_outcomes_held_and_refund(db_session: AsyncSession) -> None:
    user, _key, rows = await _seed_mixed(db_session)

    page = await usage.list_jobs(
        db_session,
        filters=usage.JobFilters(user_id=user.id),
        limit=50,
        offset=0,
        clock=_clock(),
    )

    assert page.total == 4
    by_id = {item.id: item for item in page.items}
    settled = by_id[rows[0].id]
    assert settled.accounting_outcome == "broker_settled"
    assert settled.api_key_label == "prod"
    assert settled.work_unit == "tokens"
    assert settled.refunded_wei == Decimal(830_000_000_000 - 24_070_000_000)
    assert settled.held_wei == Decimal(0)
    assert settled.duration_seconds == 60.0
    fresh = by_id[rows[2].id]
    assert fresh.accounting_outcome == "open"
    assert fresh.held_wei == Decimal(830_000_000_000)
    assert fresh.refunded_wei is None
    stale = by_id[rows[3].id]
    assert stale.accounting_outcome == "unresolved"
    assert stale.user_id is None  # customer view carries no user identity
    # Newest first.
    assert next(i.id for i in page.items) == rows[2].id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_jobs_filters_and_pages(db_session: AsyncSession) -> None:
    user, _key, _rows = await _seed_mixed(db_session)
    other, other_key = await _user(db_session, balance_wei=0, label="other")
    db_session.add(_row(other, other_key))
    await db_session.flush()

    open_only = await usage.list_jobs(
        db_session,
        filters=usage.JobFilters(user_id=user.id, state="open"),
        limit=50,
        offset=0,
        clock=_clock(),
    )
    assert open_only.total == 2
    assert all(i.state == "open" for i in open_only.items)

    vod = await usage.list_jobs(
        db_session,
        filters=usage.JobFilters(user_id=user.id, capability="video:transcode.vod"),
        limit=50,
        offset=0,
        clock=_clock(),
    )
    assert vod.total == 1
    assert vod.items[0].offering == "vod-default"

    paged = await usage.list_jobs(
        db_session,
        filters=usage.JobFilters(user_id=user.id),
        limit=1,
        offset=1,
        clock=_clock(),
    )
    assert paged.total == 4
    assert len(paged.items) == 1
    assert paged.offset == 1

    fleet = await usage.list_jobs(
        db_session,
        filters=usage.JobFilters(),
        limit=50,
        offset=0,
        clock=_clock(),
        include_user=True,
    )
    assert fleet.total == 5
    assert {i.user_email for i in fleet.items} == {user.email, other.email}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summary_groups_by_offering_key_day_and_user(db_session: AsyncSession) -> None:
    user, _key, _rows = await _seed_mixed(db_session)

    summary = await usage.summarize(
        db_session, clock=_clock(), user_id=user.id, since=None, until=None
    )

    assert summary.totals.jobs == 4
    assert summary.totals.open_jobs == 2
    assert summary.totals.closed_jobs == 2
    assert summary.totals.billed_wei == Decimal(24_070_000_000 + 200_367_000_000)
    assert summary.totals.held_wei == Decimal(830_000_000_000 + 600_000_000_000_000)
    assert summary.totals.refunded_wei == Decimal(
        (830_000_000_000 - 24_070_000_000) + (3_258_000_000_000 - 200_367_000_000)
    )
    offerings = {(o.capability, o.offering): o for o in summary.by_offering}
    chat = offerings[("openai:chat-completions", "qwen3.6-27b")]
    assert chat.jobs == 2
    assert chat.units == 29
    assert chat.work_unit == "tokens"
    assert chat.held_wei == Decimal(830_000_000_000)
    assert summary.by_offering[0].billed_wei >= summary.by_offering[-1].billed_wei
    assert summary.by_api_key[0].label == "prod"
    assert summary.by_api_key[0].jobs == 4
    days = {d.day: d for d in summary.by_day}
    assert days["2026-09-08"].jobs == 3
    assert days["2026-09-06"].billed_wei == Decimal(200_367_000_000)
    assert summary.by_user is None

    fleet = await usage.summarize(
        db_session, clock=_clock(), user_id=None, since=None, until=None, include_users=True
    )
    assert fleet.by_user is not None
    assert fleet.by_user[0].email == user.email
    assert fleet.by_user[0].held_wei == summary.totals.held_wei


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summary_honours_window(db_session: AsyncSession) -> None:
    user, _key, _rows = await _seed_mixed(db_session)
    recent = await usage.summarize(
        db_session,
        clock=_clock(),
        user_id=user.id,
        since=NOW - timedelta(hours=2),
        until=NOW,
    )
    assert recent.totals.jobs == 3
    assert recent.since == NOW - timedelta(hours=2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overview_reports_available_held_and_spent(db_session: AsyncSession) -> None:
    user, _key, _rows = await _seed_mixed(db_session)
    start = datetime(2026, 9, 8, tzinfo=UTC)

    view = await usage.overview(
        db_session, user_id=user.id, clock=_clock(), settings=_settings(cap_wei=10**15)
    )

    assert view.available_wei == Decimal(10**16)
    assert view.held_wei == Decimal(830_000_000_000 + 600_000_000_000_000)
    assert view.open_jobs == 2
    assert view.spent_period_wei == Decimal(24_070_000_000)
    assert view.spent_30d_wei == Decimal(24_070_000_000 + 200_367_000_000)
    assert view.period.cap_wei == Decimal(10**15)
    assert view.period.pct_used == pytest.approx(0.00002407)
    assert view.period.start == start
    assert [d.day for d in view.by_day] == ["2026-09-06", "2026-09-08"]

    uncapped = await usage.overview(
        db_session, user_id=user.id, clock=_clock(), settings=_settings(cap_wei=0)
    )
    assert uncapped.period.cap_wei is None
    assert uncapped.period.pct_used is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attention_lists_stale_jobs_and_refused_settlements(db_session: AsyncSession) -> None:
    user, key, rows = await _seed_mixed(db_session)
    db_session.add(
        TelemetryEvent(
            id=uuid.uuid4(),
            user_id=user.id,
            api_key_id=key.id,
            event_type=usage.SETTLEMENT_FAILURE_EVENT,
            event_schema_version=1,
            received_ts=(NOW - timedelta(minutes=10)).replace(tzinfo=None),
            source="server",
            payload={
                "session_id": str(rows[3].id),
                "protocol": "paid-session/v1",
                "code": "settlement_verification_failed",
                "reason": "missing_delegation",
            },
        )
    )
    db_session.add(
        TelemetryEvent(
            id=uuid.uuid4(),
            user_id=user.id,
            api_key_id=key.id,
            event_type=usage.SETTLEMENT_FAILURE_EVENT,
            event_schema_version=1,
            received_ts=(NOW - timedelta(days=3)).replace(tzinfo=None),
            source="server",
            payload={"reason": "old"},
        )
    )
    await db_session.flush()

    view = await usage.attention(db_session, clock=_clock(), stale_after_seconds=900)

    assert view.counts.unresolved == 1
    assert view.unresolved[0].job_id == rows[3].id
    assert view.unresolved[0].user_email == user.email
    assert view.unresolved[0].age_seconds == 3600.0
    assert view.counts.settlement_failures_24h == 1
    failure = view.settlement_failures[0]
    assert failure.reason == "missing_delegation"
    assert failure.protocol == "paid-session/v1"
    assert failure.user_email == user.email


@pytest.mark.unit
def test_wei_fields_serialize_as_integer_strings() -> None:
    """Regression: NUMERIC values with an exponent used to reach the wire as 1.20E+14."""
    balance = BalanceView(user_id=uuid.uuid4(), amount_wei=Decimal("1.20E+14"), updated_at=NOW)
    assert balance.model_dump(mode="json")["amount_wei"] == "120000000000000"

    ledger = LedgerEntryView(
        id=uuid.uuid4(),
        delta_wei=Decimal("-8.3E+11"),
        reason="payment_charge",
        related_payment_id=None,
        related_topup_id=None,
        created_at=NOW,
    )
    assert ledger.model_dump(mode="json")["delta_wei"] == "-830000000000"

    payment = PaymentView(
        id=uuid.uuid4(),
        work_id="w",
        recipient_eth_address="0x" + "11" * 20,
        capability="c",
        offering="o",
        work_units_requested=1,
        funded_value_wei=Decimal("1.20E+14"),
        expected_value_wei=Decimal(1),
        reserved_wei=Decimal(1),
        refunded_wei=Decimal(0),
        status="issued",
        created_at=NOW,
        updated_at=NOW,
    )
    assert payment.model_dump(mode="json")["funded_value_wei"] == "120000000000000"

    admin = AdminUserView(
        id=uuid.uuid4(),
        email="a@example.com",
        email_verified_at=None,
        approved=True,
        balance_wei=Decimal(10_329_766_999_983_893),
        created_at=NOW,
    )
    assert admin.model_dump(mode="json")["balance_wei"] == "10329766999983893"

    snapshot = DepositSnapshotView(
        id=uuid.uuid4(),
        taken_at=NOW,
        deposit_wei=Decimal(375_105_660_000_000_000),
        reserve_wei=Decimal(336_600_000_000_000_000),
        withdraw_round=0,
        current_round=4329,
        ticket_validity_period=2,
        ticket_validity_period_observed_at=None,
    )
    dumped = snapshot.model_dump(mode="json")
    assert dumped["deposit_wei"] == "375105660000000000"
    assert dumped["reserve_wei"] == "336600000000000000"
