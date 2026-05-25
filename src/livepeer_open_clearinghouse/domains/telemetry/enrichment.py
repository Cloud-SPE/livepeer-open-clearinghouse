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
    hostname / IP / tier lookup per event.
    """

    source_ip: str | None
    ingest_node_id: str | None
    geoip: GeoIPProvider
    # Pre-resolved account tier; the runtime queries
    # user_billing_config.tier once per batch and stamps every event.
    account_tier: str | None = None


class _BrokerOperatorCache:
    """Process-wide TTL cache mapping ``worker_url -> eth_address``.

    The registry's orchestrator list is small (handful) and stable
    over minutes, so a 60-second cache is plenty. Misses fall back to
    ``None``; the enrichment column stays NULL for that event.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._map: dict[str, str | None] = {}
        self._loaded_at: float = 0.0

    async def lookup(
        self, registry: object, worker_url: str | None
    ) -> str | None:
        if not worker_url:
            return None
        import time  # noqa: PLC0415

        now = time.monotonic()
        if not self._map or now - self._loaded_at > self._ttl:
            try:
                orcs = await registry.list_orchestrators()  # type: ignore[attr-defined]
                self._map = {o.worker_url: o.eth_address for o in orcs}
                self._loaded_at = now
            except Exception:
                # Cache miss path — keep stale entries; return None
                # for the lookup; never raise into telemetry.
                return None
        return self._map.get(worker_url)


_broker_cache = _BrokerOperatorCache()


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
    event"; database stores NULL.

    ``broker_operator_id`` is the eth_address of the orchestrator
    serving the session — looked up from the event's ``broker_url``
    payload field via the registry.
    """

    geo_region: str | None = None
    account_tier: str | None = None
    broker_operator_id: str | None = None
    ingest_node_id: str | None = None


def enrich(
    ctx: EnrichmentContext,
    *,
    payload: dict[str, object] | None = None,
    broker_operator_id: str | None = None,
) -> Enrichment:
    """Compute the enrichment bundle for one event.

    ``broker_operator_id`` is looked up by the runtime via
    :func:`enrich_with_broker_lookup`; this lower-level helper just
    stamps the value provided.
    """
    _ = payload  # reserved for future payload-specific fields
    geo = ctx.geoip.lookup(ctx.source_ip)
    return Enrichment(
        geo_region=geo,
        account_tier=ctx.account_tier,
        broker_operator_id=broker_operator_id,
        ingest_node_id=ctx.ingest_node_id,
    )


async def lookup_broker_operator(
    registry: object, broker_url: str | None
) -> str | None:
    """Public hook for the runtime to resolve a broker_url. Wraps
    the process-wide TTL cache."""
    return await _broker_cache.lookup(registry, broker_url)
