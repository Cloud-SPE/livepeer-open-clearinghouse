"""Structured JSON logging (structlog) and Prometheus metrics registry."""

from livepeer_open_clearinghouse.providers.telemetry.logging import configure_logging, get_logger
from livepeer_open_clearinghouse.providers.telemetry.metrics import (
    REGISTRY,
    auto_replenish_total,
    job_reconciliation_observations_total,
    job_terminal_accounting_total,
    metrics_middleware,
    payment_daemon_current_round,
    payment_daemon_deposit_wei,
    payment_daemon_reserve_wei,
    payment_daemon_ticket_validity_period,
    render_metrics,
    request_count,
    request_duration,
)

__all__ = [
    "REGISTRY",
    "auto_replenish_total",
    "configure_logging",
    "get_logger",
    "job_reconciliation_observations_total",
    "job_terminal_accounting_total",
    "metrics_middleware",
    "payment_daemon_current_round",
    "payment_daemon_deposit_wei",
    "payment_daemon_reserve_wei",
    "payment_daemon_ticket_validity_period",
    "render_metrics",
    "request_count",
    "request_duration",
]
