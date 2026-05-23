"""FastAPI routes for the discovery domain (app-dev surface).

All endpoints accept either an `X-API-Key` (server-side SDK callers)
or a portal session cookie (logged-in browsers viewing the Catalog
tab). Discovery is read-only and billed neutrally — it doesn't
consume credit in MVP.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from livepeer_open_clearinghouse.dependencies import AuthedUserDep, RegistryDep
from livepeer_open_clearinghouse.domains.discovery import service
from livepeer_open_clearinghouse.domains.discovery.types import (
    CapabilityList,
    OrchestratorList,
    RouteView,
)

router = APIRouter(prefix="/v1", tags=["discovery"])


@router.get("/capabilities", response_model=CapabilityList)
async def list_capabilities_endpoint(
    _user: AuthedUserDep,
    registry: RegistryDep,
) -> CapabilityList:
    items = await service.list_capabilities(registry)
    return CapabilityList(items=items)


@router.get("/orchestrators", response_model=OrchestratorList)
async def list_orchestrators_endpoint(
    _user: AuthedUserDep,
    registry: RegistryDep,
    capability: str | None = None,
) -> OrchestratorList:
    items = await service.list_orchestrators(registry, capability=capability)
    return OrchestratorList(items=items)


@router.get("/routes", response_model=RouteView)
async def select_route_endpoint(
    _user: AuthedUserDep,
    registry: RegistryDep,
    capability: str,
    offering: str,
) -> RouteView:
    """Convenience: returns a single ranked route for (capability, offering)."""
    route = await service.select_route(registry, capability=capability, offering=offering)
    if route is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no_route_available")
    return route
