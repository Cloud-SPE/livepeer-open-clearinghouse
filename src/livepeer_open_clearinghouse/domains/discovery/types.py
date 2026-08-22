"""Pydantic models for the discovery domain."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from livepeer_open_clearinghouse.providers.registry_daemon import (
    JobAxes,
    SessionAxes,
    WorkUnitEstimator,
)


class OfferingView(BaseModel):
    id: str
    price_per_work_unit_wei: Decimal | None
    work_unit: str | None
    units_per_price: int
    protocol: str
    work_unit_estimator: WorkUnitEstimator | None
    job: JobAxes | None
    session: SessionAxes | None
    # Merged node+capability extra_json metadata from the upstream
    # registry (opaque JSON object). Gateways read e.g.
    # extra["openai"]["model"] (the runner-facing serving name) and
    # workload metadata to route and rewrite request bodies.
    extra: dict[str, Any] = Field(default_factory=dict)


class CapabilityView(BaseModel):
    name: str
    work_unit: str | None
    work_unit_estimator: WorkUnitEstimator | None
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
    protocol: str
    work_unit_estimator: WorkUnitEstimator | None
    job: JobAxes | None
    session: SessionAxes | None
    # Opaque registry metadata for this route (see OfferingView.extra).
    extra: dict[str, Any] = Field(default_factory=dict)
