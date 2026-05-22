"""Unit tests for the Clock provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pymthouse.providers.clock import DefaultClock, FrozenClock


@pytest.mark.unit
def test_default_clock_returns_tz_aware_now() -> None:
    now = DefaultClock().now()
    assert now.tzinfo is not None


@pytest.mark.unit
def test_frozen_clock_advance() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    c = FrozenClock(start)
    assert c.now() == start
    c.advance(timedelta(hours=2))
    assert c.now() == start + timedelta(hours=2)


@pytest.mark.unit
def test_frozen_clock_set() -> None:
    c = FrozenClock()
    new_time = datetime(2099, 12, 31, tzinfo=UTC)
    c.set(new_time)
    assert c.now() == new_time
