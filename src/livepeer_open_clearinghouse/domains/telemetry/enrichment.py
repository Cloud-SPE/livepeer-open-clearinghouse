"""Ingest-time event enrichment.

Populates the operator-only fields on ``telemetry_event``
(``geo_region``, ``account_tier``, ``broker_operator_id``,
``ingest_node_id``) at write time. SDKs do not — and cannot — set
these; they're derived from operator-side context the SDK doesn't
have access to.

v1 scope (exec-plan 002 §"Storage strategy" → "v1 storage"):

  - ``ingest_node_id`` — always populated; sourced from settings
    (``INGEST_NODE_ID`` env) with hostname fallback.
  - ``geo_region``    — populated by a pluggable :class:`GeoIPProvider`;
    default :class:`NoopGeoIPProvider` returns ``None``. Real GeoIP
    backend ships when the operator picks a database.
  - ``account_tier``  — currently always ``None`` (no tier column on
    ``user_billing_config`` yet). Slot reserved so PR-N adds the
    column + one-line lookup without touching ingest.
  - ``broker_operator_id`` — currently always ``None`` (the registry
    doesn't expose a worker_url → operator-id reverse lookup yet).
    Slot reserved.

Enrichment is best-effort: any failure inside a provider must return
``None`` for that field, never raise.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass
from typing import Protocol


class GeoIPProvider(Protocol):
    """Resolves a source IP to a coarse region code (e.g. ``us-east``).

    Always best-effort; implementations swallow lookup failures and
    return ``None`` instead.
    """

    def lookup(self, source_ip: str | None) -> str | None: ...


class NoopGeoIPProvider:
    """Default — returns ``None`` for every IP. Used when no GeoIP
    backend is configured."""

    def lookup(self, source_ip: str | None) -> str | None:
        _ = source_ip
        return None


@dataclass(frozen=True)
class EnrichmentContext:
    """Per-event-batch context the runtime layer assembles once.

    Reused across every event in a batch so we don't repeat the
    hostname / IP lookup per event.
    """

    source_ip: str | None
    ingest_node_id: str | None
    geoip: GeoIPProvider


def resolve_ingest_node_id(configured: str | None) -> str:
    """Pick an ingest-node identifier.

    Operators can set ``INGEST_NODE_ID`` explicitly (recommended for
    multi-replica deployments where hostnames aren't meaningful);
    otherwise use the container hostname.
    """
    if configured:
        return configured
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


@dataclass(frozen=True)
class Enrichment:
    """The 4-field bundle written to ``telemetry_event``'s enrichment
    columns. ``None`` for any field means "not populated for this
    event"; database stores NULL."""

    geo_region: str | None = None
    account_tier: str | None = None
    broker_operator_id: uuid.UUID | None = None
    ingest_node_id: str | None = None


def enrich(
    ctx: EnrichmentContext,
    *,
    payload: dict[str, object] | None = None,
) -> Enrichment:
    """Compute the enrichment bundle for one event.

    Today the payload is unused; once the registry exposes a
    worker_url → operator lookup, the broker-url-bearing event types
    (``session.broker_connected``, ``server.session_janitor_finalized``)
    will pull ``broker_operator_id`` from it.
    """
    _ = payload  # reserved for the broker_url → operator lookup
    geo = ctx.geoip.lookup(ctx.source_ip)
    return Enrichment(
        geo_region=geo,
        account_tier=None,  # no tier column on user_billing_config yet
        broker_operator_id=None,  # no registry lookup yet
        ingest_node_id=ctx.ingest_node_id,
    )
