"""Structured logging configuration.

JSON output in non-dev environments; pretty console output in dev. Bind
contextual fields once (``structlog.contextvars.bind_contextvars(...)``)
and they ride along with every subsequent log call on the same request.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import Processor

from livepeer_open_clearinghouse.settings import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib logging module."""
    level_name = settings.log_level.upper()
    level_value = getattr(logging, "WARNING" if level_name == "WARN" else level_name)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level_value,
    )

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor
    if settings.app_env == "dev":
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level_value),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **bound: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, optionally pre-bound with fields."""
    log = structlog.get_logger(name)
    if bound:
        log = log.bind(**bound)
    return log
