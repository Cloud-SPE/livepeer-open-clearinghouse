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

from livepeer_open_clearinghouse.domains.discovery.service import (
    list_capabilities,
    select_route,
)
from livepeer_open_clearinghouse.providers.registry_daemon import (
    MockRegistryClient,
    SelectedRoute,
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
    assert offering.job is not None
    assert offering.job.transports == {"unary", "stream"}


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
