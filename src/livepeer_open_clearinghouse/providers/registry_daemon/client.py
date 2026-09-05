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
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_serializer,
    model_validator,
)

# Side-effect import: livepeer_open_clearinghouse._gen injects the generated-stubs dir onto
# sys.path so `from livepeer.registry.v1 import ...` resolves. Loading
# this at module level (rather than lazily in each gRPC call site) means
# anywhere in this file can do the absolute `livepeer.*` import safely.
from livepeer_open_clearinghouse import _gen  # noqa: F401

_logger = logging.getLogger(__name__)

_UINT64_MAX = (1 << 64) - 1


def _parse_uint64_decimal(value: object) -> int:
    """Parse a uint64 without allowing JSON-number precision loss."""

    if isinstance(value, bool):
        raise ValueError("uint64 decimal must not be boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        if value != "0" and value.startswith("0"):
            raise ValueError("uint64 decimal must be canonical")
        return int(value)
    raise ValueError("uint64 decimal must be an integer or canonical decimal string")


UInt64Decimal = Annotated[
    int,
    BeforeValidator(_parse_uint64_decimal),
    Field(ge=0, le=_UINT64_MAX),
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(
        {
            "type": "string",
            "pattern": r"^(0|[1-9][0-9]{0,19})$",
            "description": "Canonical decimal encoding of an unsigned 64-bit integer.",
        }
    ),
]


class JobAxes(BaseModel):
    """Known paid-job/v1 axes, preserving future minor-version additions."""

    model_config = ConfigDict(extra="allow", frozen=True)

    transports: frozenset[Literal["unary", "stream", "multipart"]] = Field(min_length=1)

    @field_serializer("transports")
    def serialize_transports(
        self, value: frozenset[Literal["unary", "stream", "multipart"]]
    ) -> list[str]:
        return sorted(value)


class SessionAxes(BaseModel):
    """Known paid-session/v1 axes, preserving future minor-version additions."""

    model_config = ConfigDict(extra="allow", frozen=True)

    descriptor_schema: str = Field(pattern=r"^[a-z][a-z0-9-]*/v[0-9]+$")
    attachment: Literal["external"] = "external"
    metering: Literal["runner-reported"]
    refill: Literal["extensible", "bounded"] = "extensible"


class SettlementKey(BaseModel):
    """Cold-key-authorized broker key accepted for settlement signatures."""

    model_config = ConfigDict(frozen=True)

    public_key: str = Field(pattern=r"^0x[0-9a-f]{130}$")
    not_before: AwareDatetime
    expires_at: AwareDatetime
    introduced_in_publication_seq: UInt64Decimal

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> SettlementKey:
        if self.expires_at <= self.not_before:
            raise ValueError("settlement key expires_at must be after not_before")
        return self


class WorkUnitEstimator(BaseModel):
    """Signed client-side funding-ceiling estimator declaration.

    LOC does not execute the estimator. It parses the registry boundary and
    relays the declaration so gateways can select their matching independent
    implementation.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    rounding: str = Field(min_length=1)
    exactness: str = Field(min_length=1)
    package: str | None = None
    fixtures: str = Field(min_length=1)


class RouteBinding(BaseModel):
    """Compact caller-stable identity for one signed selected route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_id: str = Field(min_length=1)
    quote_version: UInt64Decimal = Field(ge=1)
    constraint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RouteSnapshot(BaseModel):
    """Immutable public route declaration used to authorize one open."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["route-snapshot/v1"] = "route-snapshot/v1"
    broker_url: str = Field(min_length=1)
    eth_address: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    protocol: Literal["paid-job/v1", "paid-session/v1"]
    work_unit: str = Field(min_length=1)
    price_per_work_unit_wei: Decimal = Field(ge=0)
    units_per_price: UInt64Decimal = Field(ge=1)
    quote_id: str = Field(min_length=1)
    quote_version: UInt64Decimal = Field(ge=1)
    constraint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    settlement_keys: tuple[SettlementKey, ...]
    work_unit_estimator: WorkUnitEstimator | None = None
    job: JobAxes | None = None
    session: SessionAxes | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_protocol_axis(self) -> RouteSnapshot:
        if self.protocol == "paid-job/v1" and (self.job is None or self.session is not None):
            raise ValueError("paid-job/v1 snapshot requires job axes only")
        if self.protocol == "paid-session/v1" and (self.session is None or self.job is not None):
            raise ValueError("paid-session/v1 snapshot requires session axes only")
        return self

    @property
    def binding(self) -> RouteBinding:
        return RouteBinding(
            quote_id=self.quote_id,
            quote_version=self.quote_version,
            constraint_fingerprint=self.constraint_fingerprint,
            route_fingerprint=self.route_fingerprint,
        )


class SelectedRoute(BaseModel):
    """One concrete route — output of ``Select`` / ``SelectMany``.

    The protocol is a typed field from the signed tuple. Declared axes remain
    in ``extra`` and are parsed here, at the network boundary. Unknown axis
    fields survive so a later compatible spec minor is not silently erased.
    """

    model_config = ConfigDict(frozen=True)

    worker_url: str = Field(min_length=1)
    eth_address: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    offering: str = Field(min_length=1)
    price_per_work_unit_wei: Decimal = Field(ge=0)
    work_unit: str = Field(min_length=1)
    units_per_price: UInt64Decimal = Field(ge=1)
    quote_id: str
    quote_version: UInt64Decimal = Field(ge=1)
    constraint_fingerprint: bytes
    route_fingerprint: bytes
    protocol: Literal["paid-job/v1", "paid-session/v1"]
    settlement_keys: tuple[SettlementKey, ...] = ()
    work_unit_estimator: WorkUnitEstimator | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_protocol_axes(self) -> SelectedRoute:
        expected = "job" if self.protocol == "paid-job/v1" else "session"
        forbidden = "session" if expected == "job" else "job"
        if expected not in self.extra:
            raise ValueError(f"{self.protocol} route is missing extra.{expected}")
        if forbidden in self.extra:
            raise ValueError(f"{self.protocol} route must not declare extra.{forbidden}")
        if expected == "job":
            JobAxes.model_validate(self.extra[expected])
        else:
            SessionAxes.model_validate(self.extra[expected])
        return self

    @property
    def job(self) -> JobAxes | None:
        if self.protocol != "paid-job/v1":
            return None
        return JobAxes.model_validate(self.extra["job"])

    @property
    def session(self) -> SessionAxes | None:
        if self.protocol != "paid-session/v1":
            return None
        return SessionAxes.model_validate(self.extra["session"])

    @property
    def binding(self) -> RouteBinding:
        return RouteBinding(
            quote_id=self.quote_id,
            quote_version=self.quote_version,
            constraint_fingerprint=self.constraint_fingerprint.hex(),
            route_fingerprint=self.route_fingerprint.hex(),
        )

    def snapshot_view(self) -> RouteSnapshot:
        """Return the complete immutable declaration used at issuance."""

        return RouteSnapshot(
            broker_url=self.worker_url,
            eth_address=self.eth_address,
            capability=self.capability,
            offering=self.offering,
            protocol=self.protocol,
            work_unit=self.work_unit,
            price_per_work_unit_wei=self.price_per_work_unit_wei,
            units_per_price=self.units_per_price,
            quote_id=self.quote_id,
            quote_version=self.quote_version,
            constraint_fingerprint=self.constraint_fingerprint.hex(),
            route_fingerprint=self.route_fingerprint.hex(),
            settlement_keys=self.settlement_keys,
            work_unit_estimator=self.work_unit_estimator,
            job=self.job,
            session=self.session,
            extra=self.extra,
        )

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe immutable route declaration persisted at issuance."""

        return self.snapshot_view().model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class OfferingInfo:
    """An advertised offering on an orchestrator.

    ``extra`` is the verified capability metadata plus declared protocol axes.
    Consumers read workload-specific keys such as ``extra["openai"]["model"]``
    without treating them as protocol authority.
    """

    id: str
    price_per_work_unit_wei: Decimal | None
    work_unit: str | None
    units_per_price: int
    protocol: Literal["paid-job/v1", "paid-session/v1"]
    work_unit_estimator: WorkUnitEstimator | None
    job: JobAxes | None
    session: SessionAxes | None
    extra: dict[str, Any] = field(default_factory=dict)


def _offering_from_route(route: SelectedRoute) -> OfferingInfo:
    return OfferingInfo(
        id=route.offering,
        price_per_work_unit_wei=route.price_per_work_unit_wei,
        work_unit=route.work_unit,
        units_per_price=route.units_per_price,
        protocol=route.protocol,
        work_unit_estimator=route.work_unit_estimator,
        job=route.job,
        session=route.session,
        extra=dict(route.extra),
    )


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    """An advertised capability — name plus its offerings."""

    name: str
    work_unit: str | None
    work_unit_estimator: WorkUnitEstimator | None
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


async def select_bound_route(
    client: RegistryClient,
    *,
    capability: str,
    offering: str,
    binding: RouteBinding | None,
) -> SelectedRoute | None:
    """Select normally, or resolve the exact authoritative bound candidate."""

    if binding is None:
        return await client.select(capability, offering)
    routes = await client.select_many(capability, offering)
    return next((route for route in routes if route.binding == binding), None)


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
        protocol="paid-job/v1",
        extra={"job": {"transports": ["unary", "stream", "multipart"]}},
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
        protocol="paid-job/v1",
        extra={"job": {"transports": ["unary"]}},
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
    capability metadata and declared axes without re-parsing.
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
        protocol=proto.protocol,
        settlement_keys=tuple(
            SettlementKey(
                public_key=key.public_key,
                not_before=key.not_before,
                expires_at=key.expires_at,
                introduced_in_publication_seq=int(key.introduced_in_publication_seq),
            )
            for key in proto.settlement_keys
        ),
        work_unit_estimator=_estimator_from_proto(proto.work_unit_estimator),
        extra=_decode_extra_json(bytes(proto.extra_json)),
    )


def _estimator_from_proto(proto: object) -> WorkUnitEstimator | None:
    """Parse an optional proto estimator without inventing empty metadata."""

    estimator_id = str(getattr(proto, "id", ""))
    if not estimator_id:
        return None
    package = str(getattr(proto, "package", "")) or None
    return WorkUnitEstimator(
        id=estimator_id,
        rounding=str(getattr(proto, "rounding", "")),
        exactness=str(getattr(proto, "exactness", "")),
        package=package,
        fixtures=str(getattr(proto, "fixtures", "")),
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
        self._channel: Any | None = None
        self._stub: Any | None = None
        self._lock: Any | None = None

    async def _ensure_stub(self) -> Any:
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
        merged: dict[str, set[str]] = {}
        work_units: dict[str, str | None] = {}
        estimators: dict[str, WorkUnitEstimator | None] = {}
        for addr in addresses:
            try:
                resolved = await self._resolve(addr)
            except Exception as exc:
                _logger.debug(
                    "registry.list_capabilities.resolve_skip",
                    extra={"addr": addr, "error": str(exc)},
                )
                continue
            for node in resolved.nodes:
                for cap in node.capabilities:
                    offerings = merged.setdefault(cap.name, set())
                    work_units[cap.name] = cap.work_unit or None
                    estimators[cap.name] = _estimator_from_proto(cap.work_unit_estimator)
                    for off in cap.offerings:
                        offerings.add(off.id)
        result: list[CapabilityInfo] = []
        for name, offerings in merged.items():
            enriched: list[OfferingInfo] = []
            for offering_id in sorted(offerings):
                route = await self.select(name, offering_id)
                if route is not None:
                    enriched.append(_offering_from_route(route))
            if enriched:
                result.append(
                    CapabilityInfo(
                        name=name,
                        work_unit=work_units.get(name),
                        work_unit_estimator=estimators.get(name),
                        offerings=enriched,
                    )
                )
        return result

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        addresses = await self._list_known_addresses()
        out: list[OrchestratorInfo] = []
        for addr in addresses:
            try:
                resolved = await self._resolve(addr)
            except Exception as exc:
                _logger.debug(
                    "registry.list_orchestrators.resolve_skip",
                    extra={"addr": addr, "error": str(exc)},
                )
                continue
            for node in resolved.nodes:
                cap_views: list[CapabilityInfo] = []
                for cap in node.capabilities:
                    if capability is not None and cap.name != capability:
                        continue
                    offering_views: list[OfferingInfo] = []
                    for off in cap.offerings:
                        routes = await self.select_many(cap.name, off.id)
                        route = next((r for r in routes if r.eth_address == addr), None)
                        if route is not None:
                            offering_views.append(_offering_from_route(route))
                    if not offering_views:
                        continue
                    cap_views.append(
                        CapabilityInfo(
                            name=cap.name,
                            work_unit=cap.work_unit or None,
                            work_unit_estimator=_estimator_from_proto(cap.work_unit_estimator),
                            offerings=offering_views,
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
        self._cache: dict[tuple[Any, ...], tuple[float, object]] = {}

    def _get(self, key: tuple[Any, ...]) -> object | None:
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

    def _set(self, key: tuple[Any, ...], value: object) -> None:
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
                _offering_from_route(r),
            )
            work_units[r.capability] = r.work_unit
        return [
            CapabilityInfo(
                name=name,
                work_unit=work_units.get(name),
                work_unit_estimator=next(
                    (
                        route.work_unit_estimator
                        for route in self._routes
                        if route.capability == name and route.work_unit_estimator is not None
                    ),
                    None,
                ),
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
            caps.setdefault(r.capability, []).append(_offering_from_route(r))
            work_units[(r.eth_address, r.capability)] = r.work_unit
        return [
            OrchestratorInfo(
                eth_address=addr,
                worker_url=urls[addr],
                capabilities=[
                    CapabilityInfo(
                        name=cap_name,
                        work_unit=work_units.get((addr, cap_name)),
                        work_unit_estimator=next(
                            (
                                route.work_unit_estimator
                                for route in self._routes
                                if route.eth_address == addr
                                and route.capability == cap_name
                                and route.work_unit_estimator is not None
                            ),
                            None,
                        ),
                        offerings=offerings,
                    )
                    for cap_name, offerings in caps.items()
                ],
                signature_status="SigVerified",
                freshness_status="fresh",
            )
            for addr, caps in by_addr.items()
        ]
