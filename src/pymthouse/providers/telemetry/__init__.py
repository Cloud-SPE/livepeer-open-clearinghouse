"""Structured JSON logging (structlog) and Prometheus metrics registry."""

from pymthouse.providers.telemetry.logging import configure_logging, get_logger
from pymthouse.providers.telemetry.metrics import (
    REGISTRY,
    auto_replenish_total,
    metrics_middleware,
    payment_daemon_deposit_wei,
    payment_daemon_reserve_wei,
    render_metrics,
    request_count,
    request_duration,
)

__all__ = [
    "REGISTRY",
    "auto_replenish_total",
    "configure_logging",
    "get_logger",
    "metrics_middleware",
    "payment_daemon_deposit_wei",
    "payment_daemon_reserve_wei",
    "render_metrics",
    "request_count",
    "request_duration",
]
