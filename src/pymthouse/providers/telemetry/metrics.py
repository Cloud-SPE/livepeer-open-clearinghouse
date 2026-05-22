"""Prometheus metrics and FastAPI middleware.

A single process-wide `CollectorRegistry` holds every metric. The metrics
endpoint at `/metrics` is mounted in main.py and gated by `METRICS_TOKEN`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

REGISTRY = CollectorRegistry()

request_count = Counter(
    "pymthouse_http_requests_total",
    "Total HTTP requests handled.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

request_duration = Histogram(
    "pymthouse_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "route"),
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Operator-facing gauges for the pooled wallet's TicketBroker state. Updated
# every scheduler tick by payments.service.snapshot_deposit.
from prometheus_client import Gauge  # noqa: E402

payment_daemon_deposit_wei = Gauge(
    "pymthouse_payment_daemon_deposit_wei",
    "Current on-chain deposit (wei) of the pooled signing wallet.",
    registry=REGISTRY,
)
payment_daemon_reserve_wei = Gauge(
    "pymthouse_payment_daemon_reserve_wei",
    "Current on-chain reserve (wei) of the pooled signing wallet.",
    registry=REGISTRY,
)


async def metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Record duration and status for every request."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    # Use the matched route template (e.g. "/v1/accounts/{user_id}") when available
    # so cardinality stays bounded; fall back to the raw path otherwise.
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    request_count.labels(request.method, route_path, str(response.status_code)).inc()
    request_duration.labels(request.method, route_path).observe(elapsed)
    return response


def render_metrics() -> tuple[bytes, str]:
    """Return the Prometheus exposition payload and content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
