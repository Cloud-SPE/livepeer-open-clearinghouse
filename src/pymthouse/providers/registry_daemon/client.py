"""RegistryClient Protocol and a Mock implementation for Phase 4.

The real gRPC client lands in Phase 6/7 alongside the docker compose stack
and the ticket-mint flow. Until then, `MockRegistryClient` returns a small
hardcoded set of routes so the discovery endpoints have something to serve.

See ``docs/references/service-registry-daemon.md`` for the daemon's gRPC
surface this Protocol mirrors.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SelectedRoute:
    """One concrete route — output of ``Select`` / ``SelectMany``."""

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
    """Subset of service-registry-daemon's resolver API used by PymtHouse."""

    async def select(
        self, capability: str, offering: str
    ) -> SelectedRoute | None: ...

    async def select_many(
        self, capability: str, offering: str
    ) -> list[SelectedRoute]: ...

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
    ),
]


class MockRegistryClient:
    """Returns a small fixed set of routes. Useful only until Phase 6/7."""

    def __init__(self, routes: list[SelectedRoute] | None = None) -> None:
        self._routes = list(routes) if routes is not None else list(_SAMPLE_ROUTES)

    async def select(self, capability: str, offering: str) -> SelectedRoute | None:
        for r in self._routes:
            if r.capability == capability and r.offering == offering:
                return r
        return None

    async def select_many(
        self, capability: str, offering: str
    ) -> list[SelectedRoute]:
        return [
            r for r in self._routes
            if r.capability == capability and r.offering == offering
        ]

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

    async def list_orchestrators(
        self, *, capability: str | None = None
    ) -> list[OrchestratorInfo]:
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
