"""Tests for sessions.service.reconcile_open_sessions (janitor).

Verifies the safety-net behavior that finalizes silent sessions once
the payer-daemon reports them closed. Also covers the
mock daemon's new get_session_debits method.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import repo as _admin  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.billing.repo import CreditBalance
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.sessions import service as sessions_service
from livepeer_open_clearinghouse.domains.sessions.repo import PaymentSession
from livepeer_open_clearinghouse.domains.sessions.service import (
    SESSION_STATE_CLOSED,
    SESSION_STATE_OPEN,
)
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base
from livepeer_open_clearinghouse.providers.payment_daemon import MockPaymentDaemonClient
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    MockRegistryClient,
    SelectedRoute,
)
from livepeer_open_clearinghouse.settings import Settings


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fk(dbapi_conn: object, _: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        admin_bootstrap_token="x",
        session_signing_secret="x",
        database_url="sqlite+aiosqlite:///:memory:",
    )


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC))


async def _seed(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    uid = uuid.uuid4()
    user = User(
        id=uid,
        email=f"{uid.hex}@example.com",
        email_verified_at=datetime.now(UTC),
        password_hash="x",
    )
    key_uid = uuid.uuid4()
    key = ApiKey(
        id=key_uid,
        user_id=user.id,
        prefix=f"loc_test_{key_uid.hex[:8]}",
        hash="h",
        label="t",
    )
    db.add_all([user, key])
    await db.flush()
    db.add(CreditBalance(user_id=user.id, amount_wei=Decimal(10**12)))
    await db.flush()
    return user.id, key.id


def _route() -> SelectedRoute:
    return SelectedRoute(
        worker_url="https://broker.example/livepeer",
        eth_address="0x" + "11" * 20,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        price_per_work_unit_wei=Decimal(1000),
        work_unit="session_second",
        units_per_price=1,
        quote_id="q-1",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        extra={"interaction_mode": "session-control-plus-media@v0"},
    )


async def _open(db: AsyncSession, clock: FrozenClock):
    user_id, key_id = await _seed(db)
    registry = MockRegistryClient(routes=[_route()])
    daemon = MockPaymentDaemonClient(ev_ratio=Decimal("1.0"))
    open_resp = await sessions_service.open_session(
        db,
        user_id=user_id,
        api_key_id=key_id,
        capability="livepeer:vtuber-session",
        offering="vtuber-1080p30",
        estimated_runway_units=100,
        max_total_units=1000,
        sdk_identity=None,
        registry=registry,
        daemon=daemon,
        clock=clock,
        settings=_settings(),
    )
    return user_id, key_id, open_resp, daemon


# ---- mock daemon's get_session_debits ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_daemon_returns_empty_default_for_unknown_session() -> None:
    daemon = MockPaymentDaemonClient()
    debits = await daemon.get_session_debits(sender=b"x", work_id="unknown")
    assert debits.total_work_units == 0
    assert debits.debit_count == 0
    assert debits.closed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_daemon_set_session_debits_round_trip() -> None:
    daemon = MockPaymentDaemonClient()
    daemon.set_session_debits(
        sender=b"sender", work_id="w1", total_work_units=42, debit_count=3, closed=True
    )
    debits = await daemon.get_session_debits(sender=b"sender", work_id="w1")
    assert debits.total_work_units == 42
    assert debits.debit_count == 3
    assert debits.closed is True


# ---- reconcile_open_sessions ----


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_finalizes_silent_session_daemon_says_closed(
    db_session: AsyncSession,
) -> None:
    """Silent SDK: opened a session, never closed; daemon reports
    closed=true with total_work_units=350. Janitor finalizes."""
    clock = _clock()
    _user_id, _key_id, open_resp, daemon = await _open(db_session, clock)

    # Daemon mock: pretend the broker has closed the session having
    # debited 350 units.
    daemon.set_session_debits(
        sender=b"",
        work_id=open_resp.work_id,
        total_work_units=350,
        debit_count=4,
        closed=True,
    )

    # Advance clock so the open session's last_polled_at (NULL) is
    # eligible. NULL is always eligible per the janitor query, but
    # advance anyway for hygiene.
    clock.advance(timedelta(seconds=120))

    finalized = await sessions_service.reconcile_open_sessions(
        db_session, daemon=daemon, clock=clock
    )
    assert finalized == 1

    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_CLOSED
    assert ps.actual_units == 350
    # billed = 350 x 1000 = 350_000; funded was 1_000_000; outcome OVERFUNDED
    assert ps.billed_value_wei == Decimal(350_000)
    assert ps.outcome == "OVERFUNDED"
    assert ps.closed_at is not None
    # last_polled_at was updated even though the session was finalized
    # (we mark_polled before the close transition).
    assert ps.last_polled_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_leaves_open_session_when_daemon_says_still_open(
    db_session: AsyncSession,
) -> None:
    clock = _clock()
    _user_id, _key_id, open_resp, daemon = await _open(db_session, clock)

    daemon.set_session_debits(
        sender=b"",
        work_id=open_resp.work_id,
        total_work_units=100,
        debit_count=2,
        closed=False,
    )
    clock.advance(timedelta(seconds=120))
    finalized = await sessions_service.reconcile_open_sessions(
        db_session, daemon=daemon, clock=clock
    )
    assert finalized == 0
    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_OPEN
    assert ps.last_polled_at is not None  # but we did poll


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_skips_recently_polled_session(
    db_session: AsyncSession,
) -> None:
    """A session polled within the interval is not re-polled."""
    clock = _clock()
    _, _, open_resp, daemon = await _open(db_session, clock)

    # Pre-mark as polled "just now" so it's not eligible.
    await sessions_service.mark_polled(db_session, open_resp.session_id, clock=clock)
    daemon.set_session_debits(
        sender=b"",
        work_id=open_resp.work_id,
        total_work_units=999,
        debit_count=999,
        closed=True,  # daemon says closed, but we shouldn't see it
    )
    # Don't advance clock — within the 60s interval.
    finalized = await sessions_service.reconcile_open_sessions(
        db_session, daemon=daemon, clock=clock
    )
    assert finalized == 0
    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_OPEN  # not finalized despite daemon-closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_janitor_handles_daemon_transient_failure(
    db_session: AsyncSession,
) -> None:
    """If daemon.get_session_debits raises, the session is skipped
    (logged elsewhere) and we move on without crashing the whole pass."""
    clock = _clock()
    _, _, open_resp, _ = await _open(db_session, clock)

    class _BoomDaemon:
        async def get_session_debits(self, *, sender: bytes, work_id: str):
            raise RuntimeError("daemon down")

        async def create_payment(self, request):  # pragma: no cover
            raise NotImplementedError

        async def get_deposit_info(self):  # pragma: no cover
            raise NotImplementedError

        async def health(self) -> bool:  # pragma: no cover
            return False

    clock.advance(timedelta(seconds=120))
    finalized = await sessions_service.reconcile_open_sessions(
        db_session, daemon=_BoomDaemon(), clock=clock
    )
    assert finalized == 0
    ps = await db_session.get(PaymentSession, open_resp.session_id)
    assert ps is not None
    assert ps.state == SESSION_STATE_OPEN
    # last_polled_at NOT updated because the daemon call failed before
    # mark_polled — we skip on exception, will retry next tick.
    assert ps.last_polled_at is None
