"""Unit tests: registry `extra` metadata survives the discovery views.

Gateways depend on ``extra["openai"]["model"]`` (the runner-facing
serving name) to rewrite request bodies — an offering id selects the
route, but brokers forward bodies verbatim to runners that only accept
their own model id. Dropping ``extra`` at this boundary forces gateways
to hard-code offering→model maps.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from livepeer_open_clearinghouse.domains.discovery.service import (
    list_capabilities,
    select_route,
)
from livepeer_open_clearinghouse.providers.registry_daemon import (
    MockRegistryClient,
    RouteBinding,
    SelectedRoute,
    WorkUnitEstimator,
)

_ROUTE = SelectedRoute(
    worker_url="https://orch.example/livepeer",
    eth_address="0x3333333333333333333333333333333333333333",
    capability="openai:chat-completions",
    offering="vllm-qwen3.6-27b-default",
    price_per_work_unit_wei=Decimal("1000"),
    work_unit="tokens",
    units_per_price=1,
    quote_id="q-extra",
    quote_version=1,
    constraint_fingerprint=b"\x00" * 32,
    route_fingerprint=b"\x11" * 32,
    protocol="paid-job/v1",
    work_unit_estimator=WorkUnitEstimator(
        id="multipart-audio-duration/v1",
        rounding="ceil-to-whole-seconds",
        exactness="exact-or-reject",
        fixtures=("livepeer-network-protocol/extractors/fixtures/multipart-audio-duration-v1"),
    ),
    extra={
        "job": {"transports": ["unary", "stream"]},
        "openai": {"model": "Qwen3.6-27B", "name": "Qwen 3.6 27B"},
    },
)


@pytest.mark.unit
async def test_capability_offering_view_carries_extra() -> None:
    client = MockRegistryClient(routes=[_ROUTE])
    caps = await list_capabilities(client)
    assert len(caps) == 1
    offering = caps[0].offerings[0]
    assert offering.extra["openai"]["model"] == "Qwen3.6-27B"
    assert offering.protocol == "paid-job/v1"
    assert offering.work_unit_estimator is not None
    assert offering.work_unit_estimator.id == "multipart-audio-duration/v1"
    assert offering.job is not None
    assert offering.job.transports == {"unary", "stream"}
    assert caps[0].work_unit_estimator is not None
    assert caps[0].work_unit_estimator.id == "multipart-audio-duration/v1"
    assert caps[0].work_unit_estimator.rounding == "ceil-to-whole-seconds"
    assert caps[0].work_unit_estimator.exactness == "exact-or-reject"
    assert caps[0].work_unit_estimator.fixtures.endswith("multipart-audio-duration-v1")


@pytest.mark.unit
async def test_route_view_carries_extra() -> None:
    client = MockRegistryClient(routes=[_ROUTE])
    route = await select_route(
        client,
        capability="openai:chat-completions",
        offering="vllm-qwen3.6-27b-default",
    )
    assert route is not None
    assert route.extra["openai"]["model"] == "Qwen3.6-27B"
    assert route.work_unit_estimator is not None
    assert route.work_unit_estimator.id == "multipart-audio-duration/v1"
    assert route.route_binding.quote_id == "q-extra"
    assert route.route_binding.route_fingerprint == "11" * 32
    assert route.route_snapshot.broker_url == "https://orch.example/livepeer"
    assert route.route_snapshot.schema_version == "route-snapshot/v1"
    assert route.route_snapshot.job is not None
    assert route.route_snapshot.job.transports == {"unary", "stream"}
    assert route.route_snapshot.extra["openai"]["model"] == "Qwen3.6-27B"


@pytest.mark.unit
async def test_registry_uint64_fields_are_lossless_decimal_strings() -> None:
    uint64_max = (1 << 64) - 1
    selected = _ROUTE.model_copy(
        update={"quote_version": uint64_max, "units_per_price": uint64_max}
    )
    route = await select_route(
        MockRegistryClient(routes=[selected]),
        capability=selected.capability,
        offering=selected.offering,
    )
    assert route is not None
    payload = route.model_dump(mode="json")
    assert payload["quote_version"] == str(uint64_max)
    assert payload["units_per_price"] == str(uint64_max)
    assert payload["route_binding"]["quote_version"] == str(uint64_max)
    assert payload["route_snapshot"]["quote_version"] == str(uint64_max)
    assert payload["route_snapshot"]["units_per_price"] == str(uint64_max)

    parsed = RouteBinding.model_validate(payload["route_binding"])
    assert parsed.quote_version == uint64_max
    with pytest.raises(ValidationError):
        RouteBinding.model_validate(
            {**payload["route_binding"], "quote_version": str(uint64_max + 1)}
        )


@pytest.mark.unit
async def test_offering_view_defaults_to_empty_extra() -> None:
    route = SelectedRoute(
        worker_url=_ROUTE.worker_url,
        eth_address=_ROUTE.eth_address,
        capability="rerank",
        offering="bge-reranker",
        price_per_work_unit_wei=Decimal("10"),
        work_unit="requests",
        units_per_price=1,
        quote_id="q-bare",
        quote_version=1,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
        protocol="paid-job/v1",
        extra={"job": {"transports": ["unary"]}},
    )
    client = MockRegistryClient(routes=[route])
    caps = await list_capabilities(client)
    assert caps[0].offerings[0].extra == {"job": {"transports": ["unary"]}}
