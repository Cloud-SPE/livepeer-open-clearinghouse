"""RegistryClient Protocol and a Mock implementation for Phase 4.

The real gRPC client lands in Phase 6/7 alongside the docker compose stack
and the ticket-mint flow. Until then, `MockRegistryClient` returns a small
hardcoded set of routes so the discovery endpoints have something to serve.

See ``docs/references/service-registry-daemon.md`` for the daemon's gRPC
surface this Protocol mirrors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
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
from livepeer_open_clearinghouse.errors import DaemonUnavailable

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
    settlement_domain_id: str = Field(pattern=r"^0x[0-9a-f]{64}$")

    @model_validator(mode="after")
    def nonzero_settlement_domain(self) -> RouteBinding:
        if int(self.settlement_domain_id[2:], 16) == 0:
            raise ValueError("settlement_domain_id must be nonzero")
        return self


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
    settlement_domain_id: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    settlement_keys: tuple[SettlementKey, ...]
    work_unit_estimator: WorkUnitEstimator | None = None
    job: JobAxes | None = None
    session: SessionAxes | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_protocol_axis(self) -> RouteSnapshot:
        if int(self.settlement_domain_id[2:], 16) == 0:
            raise ValueError("settlement_domain_id must be nonzero")
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
            settlement_domain_id=self.settlement_domain_id,
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
    settlement_domain_id: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    protocol: Literal["paid-job/v1", "paid-session/v1"]
    settlement_keys: tuple[SettlementKey, ...] = ()
    work_unit_estimator: WorkUnitEstimator | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_protocol_axes(self) -> SelectedRoute:
        if int(self.settlement_domain_id[2:], 16) == 0:
            raise ValueError("settlement_domain_id must be nonzero")
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
            settlement_domain_id=self.settlement_domain_id,
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
            settlement_domain_id=self.settlement_domain_id,
            settlement_keys=self.settlement_keys,
            work_unit_estimator=self.work_unit_estimator,
            job=self.job,
            session=self.session,
            extra=self.extra,
        )

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe immutable route declaration persisted at issuance."""

        return self.snapshot_view().model_dump(mode="json")


class CatalogEstimator(BaseModel):
    """Informational estimator metadata; executable fixture references may be absent."""

    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=1)
    rounding: str = Field(min_length=1)
    exactness: str = Field(min_length=1)
    package: str | None = None
    fixtures: str | None = None


class CatalogCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)
    known_addresses: int = Field(default=0, ge=0)
    verified_compatible_addresses: int = Field(default=0, ge=0)
    confirmed_incompatible_addresses: int = Field(default=0, ge=0)
    unknown_addresses: int = Field(default=0, ge=0)
    expired_addresses: int = Field(default=0, ge=0)
    deferred_addresses: int = Field(default=0, ge=0)
    unavailable_addresses: int = Field(default=0, ge=0)


class CatalogMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    completeness: Literal["UNINITIALIZED", "PARTIAL", "COMPLETE"]
    coverage: CatalogCoverage
    snapshot_at: AwareDatetime | None = None
    evaluated_at: AwareDatetime
    discovery_scope: str
    discovery_scope_authoritative: bool
    discovery_observed_at: AwareDatetime | None = None
    discovery_valid_until: AwareDatetime | None = None
    coverage_valid_until: AwareDatetime | None = None
    stale: bool = False


class CatalogProvider(BaseModel):
    model_config = ConfigDict(frozen=True)
    eth_address: str = Field(min_length=1)
    worker_url: str = Field(min_length=1)
    worker_id: str
    selectable: bool
    exclusion_reason: str
    verified_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    health_valid_until: AwareDatetime | None = None
    expired: bool


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
    work_unit_estimator: WorkUnitEstimator | CatalogEstimator | None
    job: JobAxes | None
    session: SessionAxes | None
    extra: dict[str, Any] = field(default_factory=dict)
    provider: CatalogProvider | None = None
    constraints: dict[str, Any] = field(default_factory=dict)


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
    work_unit_estimator: WorkUnitEstimator | CatalogEstimator | None
    offerings: list[OfferingInfo]


@dataclass(frozen=True, slots=True)
class OrchestratorInfo:
    """A resolved orchestrator and what it offers."""

    eth_address: str
    worker_url: str
    capabilities: list[CapabilityInfo]
    signature_status: str
    freshness_status: str


@dataclass(frozen=True, slots=True)
class RegistryCatalog:
    capabilities: list[CapabilityInfo]
    orchestrators: list[OrchestratorInfo]
    metadata: CatalogMetadata


def _catalog_timestamp(proto: Any, field_name: str) -> datetime | None:
    if not proto.HasField(field_name):
        return None
    return getattr(proto, field_name).ToDatetime(tzinfo=UTC)  # type: ignore[no-any-return]


def _parse_catalog(proto: Any) -> RegistryCatalog:
    from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

    metadata = CatalogMetadata.model_validate(
        {
            "completeness": resolver_pb2.CatalogCompleteness.Name(proto.completeness).removeprefix(
                "CATALOG_COMPLETENESS_"
            ),
            "coverage": {
                name: getattr(proto.coverage, name) for name in CatalogCoverage.model_fields
            },
            "discovery_scope": proto.discovery_scope,
            "discovery_scope_authoritative": proto.discovery_scope_authoritative,
            **{
                name: _catalog_timestamp(proto, name)
                for name in (
                    "snapshot_at",
                    "evaluated_at",
                    "discovery_observed_at",
                    "discovery_valid_until",
                    "coverage_valid_until",
                )
            },
        }
    )
    caps: dict[str, CapabilityInfo] = {}
    orchs: dict[tuple[str, str, str], OrchestratorInfo] = {}
    for entry in proto.entries:
        # Public discovery lists only eligible entries at the daemon's evaluated_at.
        # Pricing here is informational: never construct an authorization snapshot.
        if not entry.selectable or entry.expired:
            continue
        route = entry.offering
        if route.protocol not in ("paid-job/v1", "paid-session/v1"):
            continue
        extra = json.loads(route.extra_json or b"{}")
        if not isinstance(extra, dict):
            raise ValueError("catalog extra must be an object")
        constraints = json.loads(route.constraints_json or b"{}")
        if not isinstance(constraints, dict):
            raise ValueError("catalog constraints must be an object")
        provider = CatalogProvider(
            eth_address=route.eth_address,
            worker_url=route.worker_url,
            worker_id=entry.worker_id,
            selectable=entry.selectable,
            exclusion_reason=entry.exclusion_reason,
            expired=entry.expired,
            **{
                name: _catalog_timestamp(entry, name)
                for name in ("verified_at", "valid_until", "health_valid_until")
            },
        )
        # Reuse the public axes parsers, without imposing payment quote requirements
        # on an informational catalog entry (whose quote fields may be absent).
        job = JobAxes.model_validate(extra["job"]) if route.protocol == "paid-job/v1" else None
        session = (
            SessionAxes.model_validate(extra["session"])
            if route.protocol == "paid-session/v1"
            else None
        )
        if (job is not None and "session" in extra) or (session is not None and "job" in extra):
            raise ValueError("catalog entry has conflicting protocol axes")
        price = Decimal(route.price_per_work_unit_wei)
        if not price.is_finite() or price < 0 or route.units_per_price < 1:
            raise ValueError("invalid catalog pricing")
        info = OfferingInfo(
            id=route.offering,
            price_per_work_unit_wei=price,
            work_unit=route.work_unit,
            units_per_price=route.units_per_price,
            protocol=route.protocol,
            work_unit_estimator=(
                CatalogEstimator(
                    id=route.work_unit_estimator.id,
                    rounding=route.work_unit_estimator.rounding,
                    exactness=route.work_unit_estimator.exactness,
                    package=route.work_unit_estimator.package or None,
                    fixtures=route.work_unit_estimator.fixtures or None,
                )
                if route.work_unit_estimator.id
                else None
            ),
            job=job,
            session=session,
            extra=extra,
            provider=provider,
            constraints=constraints,
        )
        cap = CapabilityInfo(route.capability, route.work_unit, info.work_unit_estimator, [info])
        if route.capability not in caps:
            caps[route.capability] = replace(cap, offerings=[])
        caps[route.capability].offerings.append(info)
        key = (route.eth_address, route.worker_url, entry.worker_id)
        if key not in orchs:
            orchs[key] = OrchestratorInfo(
                route.eth_address, route.worker_url, [], "SigVerified", "fresh"
            )
        existing = next((c for c in orchs[key].capabilities if c.name == route.capability), None)
        if existing is None:
            orchs[key].capabilities.append(cap)
        else:
            existing.offerings.append(info)
    if not caps and metadata.completeness != "COMPLETE":
        raise DaemonUnavailable(daemon="registry", reason="CATALOG_INCONCLUSIVE")
    return RegistryCatalog(list(caps.values()), list(orchs.values()), metadata)


class RegistryClient(Protocol):
    """Subset of service-registry-daemon's resolver API used by Livepeer Open Clearinghouse."""

    async def select(self, capability: str, offering: str) -> SelectedRoute | None: ...

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]: ...

    async def list_capabilities(self) -> list[CapabilityInfo]: ...

    async def list_orchestrators(
        self, *, capability: str | None = None
    ) -> list[OrchestratorInfo]: ...

    async def list_catalog(self) -> RegistryCatalog: ...

    async def health(self) -> bool: ...


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
        settlement_domain_id="0x" + "11" * 32,
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
        settlement_domain_id="0x" + "22" * 32,
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
        settlement_domain_id=proto.settlement_domain_id,
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
    Catalog reads use the daemon's network-free ListOfferings snapshot.
    The cache wrapper imposes an additional overall catalog deadline.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        selection_timeout_seconds: float = 45.0,
        discovery_rpc_timeout_seconds: float = 2.0,
    ) -> None:
        self._socket_path = socket_path
        self._selection_timeout_seconds = selection_timeout_seconds
        self._discovery_rpc_timeout_seconds = discovery_rpc_timeout_seconds
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

    async def health(self) -> bool:
        import grpc  # noqa: PLC0415
        from google.protobuf import empty_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        try:
            response = await stub.Health(empty_pb2.Empty())
        except grpc.aio.AioRpcError:
            return False
        return bool(response.chain_ok and response.manifest_fetcher_ok)

    async def select(self, capability: str, offering: str) -> SelectedRoute | None:
        import grpc  # noqa: PLC0415
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        from livepeer_open_clearinghouse.errors import DaemonUnavailable  # noqa: PLC0415

        stub = await self._ensure_stub()
        req = resolver_pb2.SelectRequest(capability=capability, offering=offering)
        try:
            resp = await stub.Select(req, timeout=self._selection_timeout_seconds)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise DaemonUnavailable(daemon="registry", reason=exc.code().name) from exc
        # A response with no .route set means no candidate (the proto leaves
        # the field unset). HasField is the safe check.
        if not resp.HasField("route"):
            return None
        return _selected_route_proto_to_dataclass(resp.route)

    async def select_many(self, capability: str, offering: str) -> list[SelectedRoute]:
        import grpc  # noqa: PLC0415
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        from livepeer_open_clearinghouse.errors import DaemonUnavailable  # noqa: PLC0415

        stub = await self._ensure_stub()
        req = resolver_pb2.SelectRequest(capability=capability, offering=offering)
        try:
            resp = await stub.SelectMany(req, timeout=self._selection_timeout_seconds)
        except grpc.aio.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return []
            raise DaemonUnavailable(daemon="registry", reason=exc.code().name) from exc
        return [_selected_route_proto_to_dataclass(r) for r in resp.routes]

    async def list_catalog(self) -> RegistryCatalog:
        import grpc  # noqa: PLC0415
        from livepeer.registry.v1 import resolver_pb2  # noqa: PLC0415

        stub = await self._ensure_stub()
        try:
            proto = await stub.ListOfferings(
                resolver_pb2.ListOfferingsRequest(), timeout=self._discovery_rpc_timeout_seconds
            )
            return _parse_catalog(proto)
        except grpc.aio.AioRpcError as exc:
            # Older daemons must be upgraded; never fall back to network crawling.
            raise DaemonUnavailable(daemon="registry", reason=exc.code().name) from exc
        except DaemonUnavailable:
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            raise DaemonUnavailable(daemon="registry", reason="CATALOG_MALFORMED") from exc

    async def list_capabilities(self) -> list[CapabilityInfo]:
        return (await self.list_catalog()).capabilities

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        return _filter_orchestrators((await self.list_catalog()).orchestrators, capability)


def _filter_orchestrators(
    items: list[OrchestratorInfo], capability: str | None
) -> list[OrchestratorInfo]:
    if capability is None:
        return items
    return [
        replace(item, capabilities=[c for c in item.capabilities if c.name == capability])
        for item in items
        if any(c.name == capability for c in item.capabilities)
    ]


class CachingRegistryClient:
    """TTL-cache wrapper around any RegistryClient.

    All four read-only methods are cached for `ttl_seconds`. ``ttl_seconds=0``
    disables stored results but retains catalog deadlines and shared refreshes.
    Only catalog methods may fall back to bounded stale results on refresh failure.
    Cache keys are scoped by method+args.

    Single-loop asyncio safe (all access is via ``await``); not thread-safe.
    """

    def __init__(
        self,
        inner: RegistryClient,
        ttl_seconds: int,
        *,
        catalog_timeout_seconds: float = 20.0,
        catalog_stale_seconds: float = 300.0,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_seconds
        self._catalog_timeout = catalog_timeout_seconds
        self._catalog_stale = catalog_stale_seconds
        self._catalog_tasks: dict[tuple[Any, ...], asyncio.Task[Any]] = {}
        self._catalog_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._generation = 0
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
        # A registry outage can appear as NOT_FOUND; retry empty selections promptly.
        if result:
            self._set(key, result)
        return result

    async def _catalog(self, key: tuple[Any, ...], fetch: Callable[[], Awaitable[Any]]) -> Any:
        entry = self._catalog_cache.get(key)
        if entry is not None and time.monotonic() < entry[0]:
            return entry[1]
        task = self._catalog_tasks.get(key)
        if task is None:
            generation = self._generation

            async def refresh() -> Any:
                try:
                    async with asyncio.timeout(self._catalog_timeout):
                        result = await fetch()
                    if self._ttl > 0 and generation == self._generation:
                        ttl = float(self._ttl)
                        if isinstance(result, RegistryCatalog):
                            bounds = [
                                t
                                for t in (
                                    result.metadata.coverage_valid_until,
                                    result.metadata.discovery_valid_until,
                                )
                                if t is not None
                            ]
                            if bounds:
                                ttl = min(
                                    ttl, max(0.0, (min(bounds) - datetime.now(UTC)).total_seconds())
                                )
                        self._catalog_cache[key] = (time.monotonic() + ttl, result)
                    return result
                except Exception as exc:
                    previous = self._catalog_cache.get(key)
                    if (
                        previous is not None
                        and time.monotonic() < previous[0] + self._catalog_stale
                    ):
                        _logger.warning("registry.catalog.stale_fallback")
                        if isinstance(previous[1], RegistryCatalog):
                            old = previous[1]
                            return replace(
                                old,
                                metadata=old.metadata.model_copy(
                                    update={"stale": True, "completeness": "PARTIAL"}
                                ),
                            )
                        return previous[1]
                    if isinstance(exc, DaemonUnavailable):
                        raise
                    raise DaemonUnavailable(
                        daemon="registry", reason="CATALOG_UNAVAILABLE"
                    ) from exc

            task = asyncio.create_task(refresh())
            self._catalog_tasks[key] = task

            def finished(done: asyncio.Task[Any]) -> None:
                if self._catalog_tasks.get(key) is done:
                    del self._catalog_tasks[key]
                # Retrieve failures even if every HTTP caller disconnected.
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def list_catalog(self) -> RegistryCatalog:
        result: RegistryCatalog = await self._catalog(("catalog",), self._inner.list_catalog)
        return result

    async def list_capabilities(self) -> list[CapabilityInfo]:
        return (await self.list_catalog()).capabilities

    async def list_orchestrators(self, *, capability: str | None = None) -> list[OrchestratorInfo]:
        return _filter_orchestrators((await self.list_catalog()).orchestrators, capability)

    def invalidate(self) -> None:
        """Drop all cache entries."""
        self._cache.clear()
        self._catalog_cache.clear()
        self._generation += 1
        self._catalog_tasks.clear()

    async def health(self) -> bool:
        return await self._inner.health()


class MockRegistryClient:
    """Returns a small fixed set of routes. Useful only until Phase 6/7."""

    def __init__(self, routes: list[SelectedRoute] | None = None) -> None:
        self._routes = list(routes) if routes is not None else list(_SAMPLE_ROUTES)

    async def list_catalog(self) -> RegistryCatalog:
        return RegistryCatalog(
            await self.list_capabilities(),
            await self.list_orchestrators(),
            CatalogMetadata(
                completeness="COMPLETE",
                coverage=CatalogCoverage(),
                evaluated_at=datetime.now(UTC),
                discovery_scope="mock",
                discovery_scope_authoritative=True,
            ),
        )

    async def health(self) -> bool:
        return True

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
