"""Business logic for discovery — thin pass-through of registry results.

No DB touched. Inputs and outputs are mapped between the registry client's
dataclasses and the domain Pydantic views in `types.py`.
"""

from __future__ import annotations

from livepeer_open_clearinghouse.domains.discovery.types import (
    CapabilityList,
    CapabilityView,
    OfferingView,
    OrchestratorList,
    OrchestratorView,
    RouteView,
)
from livepeer_open_clearinghouse.providers.registry_daemon import (
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
        units_per_price=info.units_per_price,  # type: ignore[attr-defined]
        protocol=info.protocol,  # type: ignore[attr-defined]
        work_unit_estimator=info.work_unit_estimator,  # type: ignore[attr-defined]
        job=info.job,  # type: ignore[attr-defined]
        session=info.session,  # type: ignore[attr-defined]
        extra=getattr(info, "extra", {}) or {},
        provider=getattr(info, "provider", None),
        constraints=getattr(info, "constraints", {}),
    )


def _capability(info: CapabilityInfo) -> CapabilityView:
    return CapabilityView(
        name=info.name,
        work_unit=info.work_unit,
        work_unit_estimator=info.work_unit_estimator,
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
        quote_version=r.quote_version,
        constraint_fingerprint=r.constraint_fingerprint.hex(),
        route_fingerprint=r.route_fingerprint.hex(),
        protocol=r.protocol,
        settlement_keys=r.settlement_keys,
        work_unit_estimator=r.work_unit_estimator,
        job=r.job,
        session=r.session,
        route_binding=r.binding,
        route_snapshot=r.snapshot_view(),
        extra=dict(r.extra),
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


async def capability_catalog(client: RegistryClient) -> CapabilityList:
    raw = await client.list_catalog()
    if not raw.capabilities and raw.metadata.completeness != "COMPLETE":
        from livepeer_open_clearinghouse.errors import DaemonUnavailable  # noqa: PLC0415

        raise DaemonUnavailable(daemon="registry", reason="CATALOG_INCONCLUSIVE")
    return CapabilityList(items=[_capability(c) for c in raw.capabilities], catalog=raw.metadata)


async def orchestrator_catalog(
    client: RegistryClient, *, capability: str | None
) -> OrchestratorList:
    raw = await client.list_catalog()
    items = [_orchestrator(o) for o in raw.orchestrators]
    if capability is not None:
        for item in items:
            item.capabilities = [c for c in item.capabilities if c.name == capability]
        items = [item for item in items if item.capabilities]
    if not items and raw.metadata.completeness != "COMPLETE":
        from livepeer_open_clearinghouse.errors import DaemonUnavailable  # noqa: PLC0415

        raise DaemonUnavailable(daemon="registry", reason="CATALOG_INCONCLUSIVE")
    return OrchestratorList(items=items, catalog=raw.metadata)
