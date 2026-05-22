"""Business logic for discovery — thin pass-through of registry results.

No DB touched. Inputs and outputs are mapped between the registry client's
dataclasses and the domain Pydantic views in `types.py`.
"""

from __future__ import annotations

from pymthouse.domains.discovery.types import (
    CapabilityView,
    OfferingView,
    OrchestratorView,
    RouteView,
)
from pymthouse.providers.registry_daemon import (
    CapabilityInfo,
    OrchestratorInfo,
    RegistryClient,
    SelectedRoute,
)


def _offering(info: object) -> OfferingView:
    # OfferingInfo is structurally typed; access by attribute names.
    return OfferingView(
        id=info.id,  # type: ignore[attr-defined]
        price_per_work_unit_wei=info.price_per_work_unit_wei,  # type: ignore[attr-defined]
        work_unit=info.work_unit,  # type: ignore[attr-defined]
    )


def _capability(info: CapabilityInfo) -> CapabilityView:
    return CapabilityView(
        name=info.name,
        work_unit=info.work_unit,
        offerings=[_offering(o) for o in info.offerings],
    )


def _orchestrator(info: OrchestratorInfo) -> OrchestratorView:
    return OrchestratorView(
        eth_address=info.eth_address,
        worker_url=info.worker_url,
        capabilities=[_capability(c) for c in info.capabilities],
        signature_status=info.signature_status,
        freshness_status=info.freshness_status,
    )


def _route(r: SelectedRoute) -> RouteView:
    return RouteView(
        worker_url=r.worker_url,
        eth_address=r.eth_address,
        capability=r.capability,
        offering=r.offering,
        price_per_work_unit_wei=r.price_per_work_unit_wei,
        work_unit=r.work_unit,
        units_per_price=r.units_per_price,
        quote_id=r.quote_id,
    )


async def list_capabilities(client: RegistryClient) -> list[CapabilityView]:
    raw = await client.list_capabilities()
    return [_capability(c) for c in raw]


async def list_orchestrators(
    client: RegistryClient, *, capability: str | None
) -> list[OrchestratorView]:
    raw = await client.list_orchestrators(capability=capability)
    return [_orchestrator(o) for o in raw]


async def select_route(
    client: RegistryClient, *, capability: str, offering: str
) -> RouteView | None:
    raw = await client.select(capability, offering)
    return _route(raw) if raw is not None else None
