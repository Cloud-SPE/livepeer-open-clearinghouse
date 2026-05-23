"""APScheduler integration — in-process background jobs.

Single-instance only (matches the rest of MVP). When we move to multi-
instance, this becomes the spot to swap in a distributed scheduler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from livepeer_open_clearinghouse.providers.telemetry import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_scheduler() -> AsyncIOScheduler:
    """Return the process-wide AsyncIOScheduler (not yet started)."""
    return AsyncIOScheduler(timezone="UTC")


def register_interval_job(
    func: Callable[[], Awaitable[None]],
    *,
    name: str,
    seconds: int,
    coalesce: bool = True,
    max_instances: int = 1,
) -> None:
    """Schedule `func` to run every `seconds`. Idempotent on `name`."""
    scheduler = get_scheduler()
    scheduler.add_job(
        func,
        trigger=IntervalTrigger(seconds=seconds),
        id=name,
        name=name,
        replace_existing=True,
        coalesce=coalesce,
        max_instances=max_instances,
    )
    logger.info("scheduler.job.registered", job=name, seconds=seconds)


def start_scheduler() -> None:
    """Idempotent start."""
    s = get_scheduler()
    if not s.running:
        s.start()
        logger.info("scheduler.started")


def shutdown_scheduler() -> None:
    """Stop without waiting on long-running jobs."""
    s = get_scheduler()
    if s.running:
        s.shutdown(wait=False)
        logger.info("scheduler.stopped")
