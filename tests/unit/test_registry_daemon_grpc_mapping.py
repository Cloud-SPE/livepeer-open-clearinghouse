"""Unit tests for the registry-daemon dataclass <-> proto mapping."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pymthouse import _gen  # noqa: F401  — pulls _gen onto sys.path
from pymthouse.providers.registry_daemon.client import _selected_route_proto_to_dataclass


@pytest.mark.unit
def test_selected_route_proto_to_dataclass_carries_every_field() -> None:
    from livepeer.registry.v1 import resolver_pb2

    proto = resolver_pb2.SelectedRoute(
        worker_url="https://orch.example/livepeer",
        eth_address="0x1234567890123456789012345678901234567890",
        capability="openai:chat-completions",
        offering="gpt-oss-20b",
        price_per_work_unit_wei="1000",
        work_unit="token",
        units_per_price=1,
        quote_id="q-abc",
        quote_version=7,
        constraint_fingerprint=b"\x00" * 32,
        route_fingerprint=b"\x11" * 32,
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.worker_url == "https://orch.example/livepeer"
    assert dc.eth_address == "0x1234567890123456789012345678901234567890"
    assert dc.capability == "openai:chat-completions"
    assert dc.offering == "gpt-oss-20b"
    assert dc.price_per_work_unit_wei == Decimal("1000")
    assert dc.work_unit == "token"
    assert dc.units_per_price == 1
    assert dc.quote_id == "q-abc"
    assert dc.quote_version == 7
    assert dc.constraint_fingerprint == b"\x00" * 32
    assert dc.route_fingerprint == b"\x11" * 32


@pytest.mark.unit
def test_empty_price_string_decodes_to_zero() -> None:
    from livepeer.registry.v1 import resolver_pb2

    # The proto field is `string`; an unset value materializes as "" — we
    # should treat that as zero rather than raising InvalidOperation.
    proto = resolver_pb2.SelectedRoute(
        worker_url="x", eth_address="x", capability="x", offering="x",
        price_per_work_unit_wei="", work_unit="x", units_per_price=0,
        quote_id="x", quote_version=0,
        constraint_fingerprint=b"", route_fingerprint=b"",
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.price_per_work_unit_wei == Decimal(0)


@pytest.mark.unit
def test_large_price_string_decoded_intact() -> None:
    from livepeer.registry.v1 import resolver_pb2

    # Proto stores arbitrary-precision wei as a decimal big-int string.
    huge = str(2**200 - 1)
    proto = resolver_pb2.SelectedRoute(
        worker_url="x", eth_address="x", capability="x", offering="x",
        price_per_work_unit_wei=huge, work_unit="x", units_per_price=1,
        quote_id="x", quote_version=1,
        constraint_fingerprint=b"", route_fingerprint=b"",
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert int(dc.price_per_work_unit_wei) == 2**200 - 1
