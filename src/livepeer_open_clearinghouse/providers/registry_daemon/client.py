"""RegistryClient Protocol and a Mock implementation for Phase 4.

The real gRPC client lands in Phase 6/7 alongside the docker compose stack
and the ticket-mint flow. Until then, `MockRegistryClient` returns a small
hardcoded set of routes so the discovery endpoints have something to serve.

See ``docs/references/service-registry-daemon.md`` for the daemon's gRPC
surface this Protocol mirrors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

# Side-effect import: livepeer_open_clearinghouse._gen injects the generated-stubs dir onto
# sys.path so `from livepeer.registry.v1 import ...` resolves. Loading
# this at module level (rather than lazily in each gRPC call site) means
# anywhere in this file can do the absolute `livepeer.*` import safely.
from livepeer_open_clearinghouse import _gen  # noqa: F401

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SelectedRoute:
    """One concrete route — output of ``Select`` / ``SelectMany``.

    ``extra`` carries the capability's opaque metadata block from the
    upstream registry (proto field ``extra_json``, bytes). Decoded
    once at the proto→dataclass boundary; consumers read keys like
    ``extra.get("interaction_mode")`` to drive mode selection without
    re-parsing JSON. Empty dict if the proto carries no extra blob
    or the bytes don't parse as a JSON object.
    """

    worker_url: str
    eth_address: str
    capability: str
    offering: str
    price_per_work_unit_wei: Decimal
    work_unit: str
    units_per_price: int
    quote_id: str
    quote_version: int
    constraint_fingerprint: bytes
    route_fingerprint: bytes
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def interaction_mode(self) -> str | None:
        """Convenience accessor for the upstream mode string.

        Returns ``None`` if the offering doesn't declare a mode (only
        legitimate for legacy capabilities; new offerings MUST set it
        per the upstream coordinator manifest).
        """
        raw = self.extra.get("interaction_mode")
        return raw if isinstance(raw, str) else None


@dataclass(frozen=True, slots=True)
class OfferingInfo:
    """An advertised offering on an orchestrator."""

    id: str
    price_per_work_unit_wei: Decimal | None
    work_unit: str | None


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    """An advertised capability — name plus its offerings."""

    name: str
    work_unit: str | None
    offerings: list[OfferingInfo]


@dataclass(frozen=True, slots=True)
class OrchestratorInfo:
    """A resolved orchestrator and what it offers."""

    eth_address: str
    worker_url: str
    capabilities: list[CapabilityInfo]
    signature_status: str
    freshness_status: str


class RegistryClient(Protocol):
    """Subset of service-registry-daemon's resolver API used by Livepeer Open Clearinghouse."""

    async def select(self, capability: str, offering: str) -> SelectedRoute | None: ...

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]: ...

    async def list_capabilities(self) -> list[CapabilityInfo]: ...

    async def list_orchestrators(
        self, *, capability: str | None = None
    ) -> list[OrchestratorInfo]: ...


# ---------------------------------------------------------------------------
# Mock implementation — Phase 4 stand-in
# ---------------------------------------------------------------------------


_SAMPLE_ROUTES: list[SelectedRoute] = [
    SelectedRoute(
        worker_url="https://orch-1.example/livepeer",
        eth_address="0x1111111111111111111111111111111111111111",
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        price_per_work_unit_wei=Decimal("1000"),
        work_unit="token",
        units_per_price=1,
        quote_id="mock-quote-orch1-gpt",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        extra={"interaction_mode": "http-stream@v0"},
    ),
    SelectedRoute(
        worker_url="https://orch-2.example/livepeer",
        eth_address="0x2222222222222222222222222222222222222222",
        capability="livepeer:transcoder/h264",
        offering="h264-1080p",
        price_per_work_unit_wei=Decimal("2000"),
        work_unit="frame",
        units_per_price=1,
        quote_id="mock-quote-orch2-h264",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x22" * 32,
        extra={"interaction_mode": "http-reqresp@v0"},
    ),
]


# ---------------------------------------------------------------------------
# Grpc implementation — real client over Unix domain socket
# ---------------------------------------------------------------------------


def _decode_extra_json(raw: bytes) -> dict[str, Any]:
    """Parse the proto ``extra_json`` bytes into a dict.

    Defensive: empty bytes → ``{}``; non-JSON / non-dict payloads log
    a warning and return ``{}``. The upstream coordinator-envelope
    schema guarantees a JSON object here, but we don't crash if a
    misbehaving registry sends something else.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning("registry_daemon.extra_json.parse_failed", extra={"error": str(exc)})
        return {}
    if not isinstance(parsed, dict):
        _logger.warning(
            "registry_daemon.extra_json.not_object",
            extra={"type": type(parsed).__name__},
        )
        return {}
    return parsed


def _selected_route_proto_to_dataclass(proto) -> SelectedRoute:  # type: ignore[no-untyped-def]
    """Map a proto SelectedRoute to our dataclass.

    The proto stores price as a decimal big-int *string*; we parse to
    Decimal. ``extra_json`` (bytes, JSON object) is decoded into the
    ``extra`` dict so consumers (mint, session-open) can read
    capability metadata like ``interaction_mode`` without re-parsing.
    """
    return SelectedRoute(
        worker_url=proto.worker_url,
        eth_address=proto.eth_address,
        capability=proto.capability,
        offering=proto.offering,
        price_per_work_unit_wei=Decimal(proto.price_per_work_unit_wei or "0"),
        work_unit=proto.work_unit,
        units_per_price=int(proto.units_per_price),
        quote_id=proto.quote_id,
        quote_version=int(proto.quote_version),
        constraint_fingerprint=bytes(proto.constraint_fingerprint),
        route_fingerprint=bytes(proto.route_fingerprint),
        extra=_decode_extra_json(bytes(proto.extra_json)),
    )


class GrpcRegistryClient:
    """Async gRPC client for service-registry-daemon over a Unix socket.

    Mirrors :class:`GrpcPaymentDaemonClient`'s shape: lazy stub init under
    an asyncio.Lock, single channel reused for the process lifetime.

    ``select`` / ``select_many`` map 1:1 onto the daemon's RPCs.
    ``list_capabilities`` and ``list_orchestrators`` are aggregations on
    top of ``ListKnown`` + ``ResolveByAddress`` (no flat "list all" RPC
    exists). For large registries this is O(N) RPCs — fine for MVP scale,
    flagged in tech-debt for caching.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._channel = None  # type: ignore[assignment]
        self._stub = None  # type: ignore[assignment]
        self._lock = None  # type: ignore[assignment]

    async def _ensure_stub(self):  # type: ignore[no-untyped-def]
        import asyncio  # noqa: PLC0415

        import grpc.aio  # noqa: PLC0415
        from livepeer.registry.v1 import resolver_pb2_grpc  # noqa: PLC0415

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._stub is None:
                self._channel = grpc.aio.insecure_channel(f"unix:{self._socket_path}")
                self._stub = resolver_pb2_grpc.ResolverStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def select(self, capability: str, offering: str) -> SelectedRoute | None:
        import grpc  # noqa: PLC0415
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        req = resolver_pb2.SelectRequest(capability=capability, offering=offering)
        try:
            resp = await stub.Select(req)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise
        # A response with no .route set means no candidate (the proto leaves
        # the field unset). HasField is the safe check.
        if not resp.HasField("route"):
            return None
        return _selected_route_proto_to_dataclass(resp.route)

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]:
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        req = resolver_pb2.SelectRequest(capability=capability, offering=offering)
        resp = await stub.SelectMany(req)
        return [_selected_route_proto_to_dataclass(r) for r in resp.routes]

    async def _resolve(self, eth_address: str):  # type: ignore[no-untyped-def]
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        return await stub.ResolveByAddress(
            resolver_pb2.ResolveByAddressRequest(
                eth_address=eth_address,
                allow_legacy_fallback=False,
                allow_unsigned=False,
                force_refresh=False,
            )
        )

    async def _list_known_addresses(self) -> list[str]:
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        resp = await stub.ListKnown(resolver_pb2.ListKnownRequest())
        return [e.eth_address for e in resp.entries]

    async def list_capabilities(self) -> list[CapabilityInfo]:
        # Aggregate across every known address. O(N) RPCs.
        addresses = await self._list_known_addresses()
        merged: dict[str, dict[str, OfferingInfo]] = {}
        work_units: dict[str, str | None] = {}
        for addr in addresses:
            try:
                resolved = await self._resolve(addr)
            except Exception:
                continue
            for node in resolved.nodes:
                for cap in node.capabilities:
                    offerings = merged.setdefault(cap.name, {})
                    work_units[cap.name] = cap.work_unit or None
                    for off in cap.offerings:
                        offerings.setdefault(
                            off.id,
                            OfferingInfo(
                                id=off.id,
                                price_per_work_unit_wei=(
                                    Decimal(off.price_per_work_unit_wei)
                                    if off.price_per_work_unit_wei
                                    else None
                                ),
                                work_unit=cap.work_unit or None,
                            ),
                        )
        return [
            CapabilityInfo(
                name=name,
                work_unit=work_units.get(name),
                offerings=list(offerings.values()),
            )
            for name, offerings in merged.items()
        ]

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        addresses = await self._list_known_addresses()
        out: list[OrchestratorInfo] = []
        for addr in addresses:
            try:
                resolved = await self._resolve(addr)
            except Exception:
                continue
            for node in resolved.nodes:
                cap_views: list[CapabilityInfo] = []
                for cap in node.capabilities:
                    if capability is not None and cap.name != capability:
                        continue
                    cap_views.append(
                        CapabilityInfo(
                            name=cap.name,
                            work_unit=cap.work_unit or None,
                            offerings=[
                                OfferingInfo(
                                    id=off.id,
                                    price_per_work_unit_wei=(
                                        Decimal(off.price_per_work_unit_wei)
                                        if off.price_per_work_unit_wei
                                        else None
                                    ),
                                    work_unit=cap.work_unit or None,
                                )
                                for off in cap.offerings
                            ],
                        )
                    )
                if not cap_views:
                    continue
                out.append(
                    OrchestratorInfo(
                        eth_address=node.worker_eth_address or addr,
                        worker_url=node.url,
                        capabilities=cap_views,
                        signature_status="SigVerified",  # daemon already filtered
                        freshness_status=str(resolved.freshness_status),
                    )
                )
        return out


class CachingRegistryClient:
    """TTL-cache wrapper around any RegistryClient.

    All four read-only methods are cached for `ttl_seconds`. ``ttl_seconds=0``
    disables caching (passes everything through). Cache keys are scoped
    by method+args.

    Single-loop asyncio safe (all access is via ``await``); not thread-safe.
    """

    def __init__(self, inner: RegistryClient, ttl_seconds: int) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        # Each entry: (expires_at_monotonic, value)
        self._cache: dict[tuple, tuple[float, object]] = {}

    def _get(self, key: tuple) -> object | None:
        if self._ttl <= 0:
            return None
        import time  # noqa: PLC0415

        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def _set(self, key: tuple, value: object) -> None:
        if self._ttl <= 0:
            return
        import time  # noqa: PLC0415

        self._cache[key] = (time.monotonic() + self._ttl, value)

    async def select(self, capability: str, offering: str) -> SelectedRoute | None:
        key = ("select", capability, offering)
        cached = self._get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        # None results are *not* cached — a missing route should retry on
        # the next call so a freshly-published orch becomes visible without
        # waiting out the TTL.
        result = await self._inner.select(capability, offering)
        if result is not None:
            self._set(key, result)
        return result

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]:
        key = ("select_many", capability, offering)
        cached = self._get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = await self._inner.select_many(capability, offering)
        self._set(key, result)
        return result

    async def list_capabilities(self) -> list[CapabilityInfo]:
        key = ("list_capabilities",)
        cached = self._get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = await self._inner.list_capabilities()
        self._set(key, result)
        return result

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        key = ("list_orchestrators", capability)
        cached = self._get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = await self._inner.list_orchestrators(capability=capability)
        self._set(key, result)
        return result

    def invalidate(self) -> None:
        """Drop all cache entries."""
        self._cache.clear()


class MockRegistryClient:
    """Returns a small fixed set of routes. Useful only until Phase 6/7."""

    def __init__(self, routes: list[SelectedRoute] | None = None) -> None:
        self._routes = list(routes) if routes is not None else list(_SAMPLE_ROUTES)

    async def select(self, capability: str, offering: str) -> SelectedRoute | None:
        for r in self._routes:
            if r.capability == capability and r.offering == offering:
                return r
        return None

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]:
        return [r for r in self._routes if r.capability == capability and r.offering == offering]

    async def list_capabilities(self) -> list[CapabilityInfo]:
        by_name: dict[str, dict[str, OfferingInfo]] = {}
        work_units: dict[str, str | None] = {}
        for r in self._routes:
            offerings = by_name.setdefault(r.capability, {})
            offerings.setdefault(
                r.offering,
                OfferingInfo(
                    id=r.offering,
                    price_per_work_unit_wei=r.price_per_work_unit_wei,
                    work_unit=r.work_unit,
                ),
            )
            work_units[r.capability] = r.work_unit
        return [
            CapabilityInfo(
                name=name,
                work_unit=work_units.get(name),
                offerings=list(offerings.values()),
            )
            for name, offerings in by_name.items()
        ]

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        by_addr: dict[str, dict[str, list[OfferingInfo]]] = {}
        urls: dict[str, str] = {}
        work_units: dict[tuple[str, str], str | None] = {}
        for r in self._routes:
            if capability is not None and r.capability != capability:
                continue
            urls[r.eth_address] = r.worker_url
            caps = by_addr.setdefault(r.eth_address, {})
            caps.setdefault(r.capability, []).append(
                OfferingInfo(
                    id=r.offering,
                    price_per_work_unit_wei=r.price_per_work_unit_wei,
                    work_unit=r.work_unit,
                )
            )
            work_units[(r.eth_address, r.capability)] = r.work_unit
        return [
            OrchestratorInfo(
                eth_address=addr,
                worker_url=urls[addr],
                capabilities=[
                    CapabilityInfo(
                        name=cap_name,
                        work_unit=work_units.get((addr, cap_name)),
                        offerings=offerings,
                    )
                    for cap_name, offerings in caps.items()
                ],
                signature_status="SigVerified",
                freshness_status="fresh",
            )
            for addr, caps in by_addr.items()
        ]
