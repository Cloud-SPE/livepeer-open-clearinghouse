"""Clock Protocol — injected source of `now()` so tests can freeze time."""

from pymthouse.providers.clock.clock import Clock, DefaultClock, FrozenClock

__all__ = ["Clock", "DefaultClock", "FrozenClock"]
