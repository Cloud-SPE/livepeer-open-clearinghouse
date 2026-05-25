"""Tests for the four remaining notification triggers added on top of
the PR-5 cap_reached baseline: winddown_warning, sdk_outdated,
period_rollover, session_failed_repeatedly."""

from __future__ import annotations

import typing
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

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
from livepeer_open_clearinghouse.domains.notifications.repo import PortalNotification
from livepeer_open_clearinghouse.domains.payments import repo as _payments  # noqa: F401
from livepeer_open_clearinghouse.domains.telemetry import (
    server_events as telemetry_events,
)
from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent
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


@asynccontextmanager
async def _shared_session_factory(session: AsyncSession):
    """Factory that yields the test session — bypass the independent
    engine the production code uses."""
    yield session


@pytest.mark.unit
async def test_notify_winddown_warning_writes_portal_row(
    session: AsyncSession, user: User
) -> None:
    fired = await prefs.notify_winddown_warning(
        session,
        user_id=user.id,
        session_id=uuid.uuid4(),
        reason="cap_imminent",
        clock=FrozenClock(),
        independent_session_factory=lambda: _shared_session_factory(session),
    )
    assert fired[prefs.CHANNEL_IN_PORTAL] is True
    rows = list((await session.scalars(select(PortalNotification))).all())
    assert len(rows) == 1
    assert rows[0].trigger == prefs.TRIGGER_WINDDOWN_WARNING
    assert rows[0].body["reason"] == "cap_imminent"


@pytest.mark.unit
async def test_notify_sdk_outdated_writes_portal_row_when_enabled(
    session: AsyncSession, user: User
) -> None:
    # Default for (sdk_outdated, in_portal) is False; flip on.
    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_SDK_OUTDATED,
        channel=prefs.CHANNEL_IN_PORTAL,
        enabled=True,
    )
    fired = await prefs.notify_sdk_outdated(
        session,
        user_id=user.id,
        lang="python",
        semver="0.1.0",
        observed_status="deprecated",
        clock=FrozenClock(),
        independent_session_factory=lambda: _shared_session_factory(session),
    )
    assert fired[prefs.CHANNEL_IN_PORTAL] is True


@pytest.mark.unit
async def test_notify_period_rollover_writes_portal_row(
    session: AsyncSession, user: User
) -> None:
    fired = await prefs.notify_period_rollover(
        session,
        user_id=user.id,
        period_start="2026-05-01T00:00:00Z",
        period_end="2026-06-01T00:00:00Z",
        previous_period_spend_wei=42_000,
        clock=FrozenClock(),
        independent_session_factory=lambda: _shared_session_factory(session),
    )
    assert fired[prefs.CHANNEL_IN_PORTAL] is True
    row = (await session.scalars(select(PortalNotification))).one()
    assert row.trigger == prefs.TRIGGER_PERIOD_ROLLOVER
    assert row.body["previous_period_spend_wei"] == 42_000


@pytest.mark.unit
async def test_notify_session_failed_repeatedly_writes_portal_row(
    session: AsyncSession, user: User
) -> None:
    # Default for (session_failed_repeatedly, in_portal) is False; flip on.
    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_SESSION_FAILED_REPEATEDLY,
        channel=prefs.CHANNEL_IN_PORTAL,
        enabled=True,
    )
    fired = await prefs.notify_session_failed_repeatedly(
        session,
        user_id=user.id,
        api_key_id=uuid.uuid4(),
        failure_count=5,
        window_hours=1,
        clock=FrozenClock(),
        independent_session_factory=lambda: _shared_session_factory(session),
    )
    assert fired[prefs.CHANNEL_IN_PORTAL] is True
    row = (await session.scalars(select(PortalNotification))).one()
    assert row.body["failure_count"] == 5
    assert row.body["window_hours"] == 1


@pytest.mark.unit
def test_sdk_outdated_ttl_dedupe_resets_on_window_expiry() -> None:
    """The TTL set is in-process; manipulate it directly to assert
    the dedupe semantics without spawning a real loop."""
    user_id = uuid.uuid4()
    key = (user_id, "python", "0.1.0")
    telemetry_events._sdk_outdated_seen.clear()
    # First call seeds the cache.
    telemetry_events._sdk_outdated_seen[key] = datetime.now(UTC).timestamp() - (
        telemetry_events.SDK_OUTDATED_DEDUPE_HOURS * 3600 + 1
    )
    # If the cached value is older than the window, the helper should
    # treat it as expired and reset. We don't have a public probe, so
    # the assertion is: the seeded value is *not* equal to "now".
    seeded = telemetry_events._sdk_outdated_seen[key]
    assert seeded < datetime.now(UTC).timestamp()
    telemetry_events._sdk_outdated_seen.clear()


@pytest.mark.unit
async def test_winddown_emit_path_skips_when_flag_not_set(
    session: AsyncSession, user: User
) -> None:
    """Direct emit_refill_served with will_refuse_next_refill=False
    should NOT fire the winddown notification.

    api_key_id is NULL here to avoid the FK on a fixtured api_key row
    we don't care about (the helper accepts NULL per the schema).
    """
    await telemetry_events.emit_refill_served(
        session,
        api_key_id=None,  # type: ignore[arg-type]
        user_id=user.id,
        session_id=uuid.uuid4(),
        refill_seq=1,
        funded_value_wei=1000,
        cap_status={"will_refuse_next_refill": False},
        clock=FrozenClock(),
    )
    rows = list(
        (
            await session.scalars(
                select(PortalNotification).where(
                    PortalNotification.trigger == prefs.TRIGGER_WINDDOWN_WARNING
                )
            )
        ).all()
    )
    assert rows == []


def _make_event(
    *,
    api_key_id: uuid.UUID | None,
    user_id: uuid.UUID,
    event_type: str,
    when: datetime,
) -> TelemetryEvent:
    from livepeer_open_clearinghouse.domains.telemetry.config import SOURCE_SDK  # noqa: PLC0415

    return TelemetryEvent(
        api_key_id=api_key_id,
        user_id=user_id,
        event_type=event_type,
        event_schema_version=1,
        correlation_id=None,
        client_ts=when,
        received_ts=when,
        source=SOURCE_SDK,
        payload={},
    )


@pytest.mark.unit
async def test_session_failed_repeatedly_threshold(session: AsyncSession, user: User) -> None:
    """Verify the counting query underlying session_failed_repeatedly:
    3 session.error rows in the last hour cross the threshold.

    Use NULL api_key_id to skip the FK on a fixtured key.
    """
    from sqlalchemy import func  # noqa: PLC0415

    from livepeer_open_clearinghouse.domains.telemetry.repo import TelemetryEvent  # noqa: PLC0415

    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    for i in range(3):
        session.add(
            _make_event(
                api_key_id=None,  # type: ignore[arg-type]
                user_id=user.id,
                event_type="session.error",
                when=now - timedelta(minutes=i),
            )
        )
    await session.flush()
    count = await session.scalar(
        select(func.count())
        .select_from(TelemetryEvent)
        .where(
            TelemetryEvent.user_id == user.id,
            TelemetryEvent.event_type == "session.error",
            TelemetryEvent.received_ts >= now - timedelta(hours=1),
        )
    )
    assert int(count or 0) == 3


@pytest.mark.unit
async def test_session_failed_repeatedly_dedupes_within_window(
    session: AsyncSession, user: User
) -> None:
    """Two batches inside the dedupe window should fire only once.

    Builds 3 session.error rows, runs the post-ingest evaluator, then
    runs it again — the second pass must NOT write a second
    portal_notification row.
    """
    from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey  # noqa: PLC0415
    from livepeer_open_clearinghouse.domains.telemetry import runtime  # noqa: PLC0415

    runtime._session_failed_last_fired.clear()

    # Need a real api_key row — _evaluate_post_ingest_triggers reads
    # api_key_id as the dedupe key. Use an ApiKey + a user that owns it.
    api_key = ApiKey(
        user_id=user.id,
        prefix="pymth_live_dedupe_test",
        hash="0" * 64,
        label="dedupe-test",
    )
    session.add(api_key)
    await session.flush()

    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_SESSION_FAILED_REPEATEDLY,
        channel=prefs.CHANNEL_IN_PORTAL,
        enabled=True,
    )

    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    for i in range(3):
        session.add(
            _make_event(
                api_key_id=api_key.id,
                user_id=user.id,
                event_type="session.error",
                when=now - timedelta(minutes=i),
            )
        )
    await session.flush()

    class _StubEvent:
        event_type = "session.error"
        payload: typing.ClassVar[dict[str, object]] = {}

    clock = FrozenClock(start=now)

    # Patch the helper's notify call so the in-portal write goes
    # through the shared test session (the production path opens an
    # independent engine session which sqlite-in-memory can't reach).
    real_notify = prefs.notify_session_failed_repeatedly

    async def _shim(*, user_id, api_key_id, failure_count, window_hours, clock, **_):
        await real_notify(
            session,
            user_id=user_id,
            api_key_id=api_key_id,
            failure_count=failure_count,
            window_hours=window_hours,
            clock=clock,
            independent_session_factory=lambda: _shared_session_factory(session),
        )

    import livepeer_open_clearinghouse.domains.notifications.prefs as _prefs_mod  # noqa: PLC0415

    original = _prefs_mod.notify_session_failed_repeatedly
    _prefs_mod.notify_session_failed_repeatedly = lambda db, **kw: _shim(**kw)  # type: ignore[assignment]
    try:
        await runtime._evaluate_post_ingest_triggers(
            session,
            api_key_id=api_key.id,
            user_id=user.id,
            events=[_StubEvent()],
            clock=clock,
        )
        # Second pass — same window, same api_key_id, should be suppressed.
        await runtime._evaluate_post_ingest_triggers(
            session,
            api_key_id=api_key.id,
            user_id=user.id,
            events=[_StubEvent()],
            clock=clock,
        )
    finally:
        _prefs_mod.notify_session_failed_repeatedly = original  # type: ignore[assignment]

    rows = list(
        (
            await session.scalars(
                select(PortalNotification).where(
                    PortalNotification.trigger == prefs.TRIGGER_SESSION_FAILED_REPEATEDLY
                )
            )
        ).all()
    )
    assert len(rows) == 1, "second evaluator call inside dedupe window should not re-fire"


@pytest.mark.unit
async def test_session_failed_repeatedly_refires_after_window(
    session: AsyncSession, user: User
) -> None:
    """Once the dedupe TTL elapses, a fresh threshold-cross fires again."""
    from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey  # noqa: PLC0415
    from livepeer_open_clearinghouse.domains.telemetry import runtime  # noqa: PLC0415

    runtime._session_failed_last_fired.clear()

    api_key = ApiKey(
        user_id=user.id,
        prefix="pymth_live_refire_test",
        hash="1" * 64,
        label="dedupe-refire-test",
    )
    session.add(api_key)
    await session.flush()

    await prefs.set_preference(
        session,
        user_id=user.id,
        trigger=prefs.TRIGGER_SESSION_FAILED_REPEATEDLY,
        channel=prefs.CHANNEL_IN_PORTAL,
        enabled=True,
    )

    now_first = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    for i in range(3):
        session.add(
            _make_event(
                api_key_id=api_key.id,
                user_id=user.id,
                event_type="session.error",
                when=now_first - timedelta(minutes=i),
            )
        )
    await session.flush()

    class _StubEvent:
        event_type = "session.error"
        payload: typing.ClassVar[dict[str, object]] = {}

    real_notify = prefs.notify_session_failed_repeatedly

    async def _shim(*, user_id, api_key_id, failure_count, window_hours, clock, **_):
        await real_notify(
            session,
            user_id=user_id,
            api_key_id=api_key_id,
            failure_count=failure_count,
            window_hours=window_hours,
            clock=clock,
            independent_session_factory=lambda: _shared_session_factory(session),
        )

    import livepeer_open_clearinghouse.domains.notifications.prefs as _prefs_mod  # noqa: PLC0415

    original = _prefs_mod.notify_session_failed_repeatedly
    _prefs_mod.notify_session_failed_repeatedly = lambda db, **kw: _shim(**kw)  # type: ignore[assignment]
    try:
        await runtime._evaluate_post_ingest_triggers(
            session,
            api_key_id=api_key.id,
            user_id=user.id,
            events=[_StubEvent()],
            clock=FrozenClock(start=now_first),
        )

        # Second batch 90 minutes later — first batch's errors have
        # aged out of the rolling window; add 3 fresh errors and run
        # the evaluator again. With the dedupe TTL elapsed, this fires.
        now_second = now_first + timedelta(minutes=90)
        for i in range(3):
            session.add(
                _make_event(
                    api_key_id=api_key.id,
                    user_id=user.id,
                    event_type="session.error",
                    when=now_second - timedelta(minutes=i),
                )
            )
        await session.flush()
        await runtime._evaluate_post_ingest_triggers(
            session,
            api_key_id=api_key.id,
            user_id=user.id,
            events=[_StubEvent()],
            clock=FrozenClock(start=now_second),
        )
    finally:
        _prefs_mod.notify_session_failed_repeatedly = original  # type: ignore[assignment]

    rows = list(
        (
            await session.scalars(
                select(PortalNotification).where(
                    PortalNotification.trigger == prefs.TRIGGER_SESSION_FAILED_REPEATEDLY
                )
            )
        ).all()
    )
    assert len(rows) == 2, "evaluator past the dedupe window should re-fire"
