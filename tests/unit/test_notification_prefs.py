"""Tests for the notification-preferences system + cap_reached
trigger fan-out."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from livepeer_open_clearinghouse.domains.accounts import repo as _accounts  # noqa: F401
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.api_keys import repo as _api_keys  # noqa: F401
from livepeer_open_clearinghouse.domains.billing import repo as _billing  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications import prefs
from livepeer_open_clearinghouse.domains.notifications import repo as _notif  # noqa: F401
from livepeer_open_clearinghouse.domains.notifications.repo import (
    NotificationConfig,
    PortalNotification,
)
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.usage import repo as _usage  # noqa: F401
from livepeer_open_clearinghouse.providers.clock import FrozenClock
from livepeer_open_clearinghouse.providers.db.base import Base


@pytest_asyncio.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @sa_event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn: object, _: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture()
async def user(session: AsyncSession) -> User:
    u = User(email="u@example.com")
    session.add(u)
    await session.flush()
    return u


class _RecordingEmailProvider:
    """Captures every send for assertion."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, message) -> str:
        self.sent.append((message.to, message.subject))
        return "test-message-id"


@pytest.mark.unit
async def test_resolved_prefs_returns_defaults_when_no_overrides(
    session: AsyncSession, user: User
) -> None:
    out = await prefs.resolved_prefs_for_user(session, user_id=user.id)
    # cap_reached + email is True by default
    assert out[(prefs.TRIGGER_CAP_REACHED, prefs.CHANNEL_EMAIL)] is True
    # cap_reached + webhook is False by default (opt-in only)
    assert out[(prefs.TRIGGER_CAP_REACHED, prefs.CHANNEL_WEBHOOK)] is False
    # period_rollover + email is False by default
    assert out[(prefs.TRIGGER_PERIOD_ROLLOVER, prefs.CHANNEL_EMAIL)] is False
    # Every (trigger, channel) cell is present
    assert len(out) == len(prefs.ALL_TRIGGERS) * len(prefs.ALL_CHANNELS)


@pytest.mark.unit
async def test_set_preference_overrides_default(session: AsyncSession, user: User) -> None:
    # Default for cap_reached+email is True; flip to False
    row = await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_CAP_REACHED,
        channel=prefs.CHANNEL_EMAIL,
        enabled=False,
    )
    assert row.enabled is False
    out = await prefs.resolved_prefs_for_user(session, user_id=user.id)
    assert out[(prefs.TRIGGER_CAP_REACHED, prefs.CHANNEL_EMAIL)] is False


@pytest.mark.unit
async def test_set_preference_upserts(session: AsyncSession, user: User) -> None:
    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_CAP_REACHED,
        channel=prefs.CHANNEL_EMAIL,
        enabled=False,
    )
    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_CAP_REACHED,
        channel=prefs.CHANNEL_EMAIL,
        enabled=True,
    )
    rows = list((await session.scalars(select(NotificationConfig))).all())
    assert len(rows) == 1
    assert rows[0].enabled is True


@pytest.mark.unit
async def test_set_preference_rejects_unknown_trigger(session: AsyncSession, user: User) -> None:
    with pytest.raises(prefs.InvalidTrigger):
        await prefs.set_preference(
            session,
            user_id=user.id,
            trigger="not_a_trigger",
            channel=prefs.CHANNEL_EMAIL,
            enabled=True,
        )


@pytest.mark.unit
async def test_set_preference_rejects_unknown_channel(session: AsyncSession, user: User) -> None:
    with pytest.raises(prefs.InvalidChannel):
        await prefs.set_preference(
            session,
            user_id=user.id,
            trigger=prefs.TRIGGER_CAP_REACHED,
            channel="carrier_pigeon",
            enabled=True,
        )


@pytest.mark.unit
async def test_notify_fires_email_and_portal_by_default(session: AsyncSession, user: User) -> None:
    provider = _RecordingEmailProvider()
    fired = await prefs.notify(
        session,
        trigger=prefs.TRIGGER_CAP_REACHED,
        user_id=user.id,
        user_email=user.email,
        subject="Cap reached",
        body={"which_cap": "user_balance", "remaining_wei": 0},
        clock=FrozenClock(datetime(2026, 5, 24, 12, 0, tzinfo=UTC)),
        email_provider=provider,
    )
    assert fired == {
        prefs.CHANNEL_EMAIL: True,
        prefs.CHANNEL_IN_PORTAL: True,
    }
    assert len(provider.sent) == 1
    to, subject = provider.sent[0]
    assert to == "u@example.com"
    assert subject == "Cap reached"
    portal_rows = list((await session.scalars(select(PortalNotification))).all())
    assert len(portal_rows) == 1
    assert portal_rows[0].trigger == prefs.TRIGGER_CAP_REACHED
    assert portal_rows[0].dismissed_at is None


@pytest.mark.unit
async def test_notify_respects_user_disable(session: AsyncSession, user: User) -> None:
    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_CAP_REACHED,
        channel=prefs.CHANNEL_EMAIL,
        enabled=False,
    )
    provider = _RecordingEmailProvider()
    fired = await prefs.notify(
        session,
        trigger=prefs.TRIGGER_CAP_REACHED,
        user_id=user.id,
        user_email=user.email,
        subject="Cap reached",
        body={},
        clock=FrozenClock(),
        email_provider=provider,
    )
    assert fired.get(prefs.CHANNEL_EMAIL) is None
    assert fired.get(prefs.CHANNEL_IN_PORTAL) is True
    assert provider.sent == []


@pytest.mark.unit
async def test_notify_swallows_email_provider_failure(session: AsyncSession, user: User) -> None:
    class _BrokenProvider:
        async def send(self, message) -> str:
            raise RuntimeError("simulated send failure")

    fired = await prefs.notify(
        session,
        trigger=prefs.TRIGGER_CAP_REACHED,
        user_id=user.id,
        user_email=user.email,
        subject="Cap reached",
        body={},
        clock=FrozenClock(),
        email_provider=_BrokenProvider(),
    )
    # in_portal still wrote successfully; email returned False rather than raising
    assert fired[prefs.CHANNEL_EMAIL] is False
    assert fired[prefs.CHANNEL_IN_PORTAL] is True


@pytest.mark.unit
async def test_list_active_excludes_dismissed(session: AsyncSession, user: User) -> None:
    clock = FrozenClock(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))
    a = PortalNotification(
        user_id=user.id, trigger=prefs.TRIGGER_CAP_REACHED, body={}, fired_at=clock.now()
    )
    b = PortalNotification(
        user_id=user.id,
        trigger=prefs.TRIGGER_CAP_REACHED,
        body={},
        fired_at=clock.now(),
        dismissed_at=clock.now(),
    )
    session.add_all([a, b])
    await session.flush()

    rows = await prefs.list_active_portal_notifications(session, user_id=user.id)
    assert len(rows) == 1
    assert rows[0].id == a.id


@pytest.mark.unit
async def test_dismiss_marks_row(session: AsyncSession, user: User) -> None:
    clock = FrozenClock(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))
    row = PortalNotification(
        user_id=user.id, trigger=prefs.TRIGGER_CAP_REACHED, body={}, fired_at=clock.now()
    )
    session.add(row)
    await session.flush()

    result = await prefs.dismiss_portal_notification(
        session, user_id=user.id, notification_id=row.id, clock=clock
    )
    assert result.dismissed_at is not None


@pytest.mark.unit
async def test_dismiss_other_users_row_not_found(session: AsyncSession, user: User) -> None:
    other = User(email="other@example.com")
    session.add(other)
    await session.flush()
    row = PortalNotification(
        user_id=other.id, trigger=prefs.TRIGGER_CAP_REACHED, body={}, fired_at=datetime.now(UTC)
    )
    session.add(row)
    await session.flush()

    with pytest.raises(prefs.PortalNotificationNotFound):
        await prefs.dismiss_portal_notification(
            session, user_id=user.id, notification_id=row.id, clock=FrozenClock()
        )


@pytest.mark.unit
async def test_notify_cap_reached_resolves_user(session: AsyncSession, user: User) -> None:
    """End-to-end through notify_cap_reached: looks up user, fires
    in-portal banner via the injected independent session factory.

    In production the factory opens against the global engine so the
    write survives the outer request's rollback. In tests we inject a
    factory that yields the test session — same wiring, simpler
    fixture.
    """

    @asynccontextmanager
    async def _factory():
        yield session

    fired = await prefs.notify_cap_reached(
        session,
        user_id=user.id,
        which_cap="user_balance",
        remaining_wei=0,
        clock=FrozenClock(),
        independent_session_factory=_factory,
    )
    assert fired[prefs.CHANNEL_IN_PORTAL] is True
    rows = list((await session.scalars(select(PortalNotification))).all())
    assert len(rows) == 1
    assert rows[0].body["which_cap"] == "user_balance"


@pytest.mark.unit
async def test_notify_cap_reached_silent_for_missing_user(
    session: AsyncSession,
) -> None:
    fired = await prefs.notify_cap_reached(
        session,
        user_id=uuid.uuid4(),  # no such user
        which_cap="user_balance",
        remaining_wei=0,
        clock=FrozenClock(),
    )
    assert fired == {}
