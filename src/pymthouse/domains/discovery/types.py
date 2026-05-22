"""Pydantic models for the discovery domain."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class OfferingView(BaseModel):
    id: str
    price_per_work_unit_wei: Decimal | None
    work_unit: str | None


class CapabilityView(BaseModel):
    name: str
    work_unit: str | None
    offerings: list[OfferingView]


class CapabilityList(BaseModel):
    items: list[CapabilityView]


class OrchestratorView(BaseModel):
    eth_address: str
    worker_url: str
    capabilities: list[CapabilityView]
    signature_status: str
    freshness_status: str


class OrchestratorList(BaseModel):
    items: list[OrchestratorView]


class RouteView(BaseModel):
    """A single selected route — what `Select()` returns, web-flavored."""

    worker_url: str
    eth_address: str
    capability: str
    offering: str
    price_per_work_unit_wei: Decimal
    work_unit: str
    units_per_price: int
    quote_id: str
