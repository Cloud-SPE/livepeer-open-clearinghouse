"""Clock Protocol and implementations.

Inject `Clock` into services and use `clock.now()` instead of calling
`datetime.now(UTC)` directly. Tests substitute `FrozenClock` to make
time-dependent logic deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """A source of `now()`."""

    def now(self) -> datetime: ...


class DefaultClock:
    """Wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """A test clock. `advance(delta)` to move time forward."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, when: datetime) -> None:
        self._now = when
