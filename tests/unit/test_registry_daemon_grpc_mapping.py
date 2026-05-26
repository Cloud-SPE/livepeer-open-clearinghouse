"""Unit tests for the registry-daemon dataclass <-> proto mapping."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from livepeer_open_clearinghouse import _gen  # noqa: F401  — pulls _gen onto sys.path
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    SelectedRoute,
    _decode_extra_json,
    _selected_route_proto_to_dataclass,
)


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
        worker_url="x",
        eth_address="x",
        capability="x",
        offering="x",
        price_per_work_unit_wei="",
        work_unit="x",
        units_per_price=0,
        quote_id="x",
        quote_version=0,
        constraint_fingerprint=b"",
        route_fingerprint=b"",
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.price_per_work_unit_wei == Decimal(0)


@pytest.mark.unit
def test_extra_json_decoded_into_extra_dict() -> None:
    """Upstream `extra_json` carries `interaction_mode` (and other
    capability metadata). The dataclass surfaces it via `extra` so
    consumers don't re-parse JSON, plus an `interaction_mode`
    convenience property."""
    from livepeer.registry.v1 import resolver_pb2

    proto = resolver_pb2.SelectedRoute(
        worker_url="x",
        eth_address="x",
        capability="openai:realtime",
        offering="openai-resale",
        price_per_work_unit_wei="0",
        work_unit="audio_second",
        units_per_price=1,
        quote_id="x",
        quote_version=1,
        constraint_fingerprint=b"",
        route_fingerprint=b"",
        extra_json=json.dumps(
            {
                "interaction_mode": "ws-realtime@v0",
                "max_session_seconds": 3600,
                "category": "audio",
            }
        ).encode("utf-8"),
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.extra["interaction_mode"] == "ws-realtime@v0"
    assert dc.extra["max_session_seconds"] == 3600
    assert dc.interaction_mode == "ws-realtime@v0"


@pytest.mark.unit
def test_missing_extra_json_yields_empty_dict() -> None:
    """A proto with no extra_json (empty bytes) maps to {} — no crash,
    no surprise."""
    from livepeer.registry.v1 import resolver_pb2

    proto = resolver_pb2.SelectedRoute(
        worker_url="x",
        eth_address="x",
        capability="x",
        offering="x",
        price_per_work_unit_wei="0",
        work_unit="x",
        units_per_price=0,
        quote_id="x",
        quote_version=0,
        constraint_fingerprint=b"",
        route_fingerprint=b"",
        extra_json=b"",
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.extra == {}
    assert dc.interaction_mode is None


@pytest.mark.unit
def test_extra_json_invalid_payloads_do_not_crash() -> None:
    """Defense-in-depth: malformed or non-object JSON is logged and
    returned as empty dict rather than raising."""
    # Invalid JSON
    assert _decode_extra_json(b"not json {{{") == {}
    # Valid JSON but wrong shape (list, scalar)
    assert _decode_extra_json(b'["a", "b"]') == {}
    assert _decode_extra_json(b"42") == {}
    # Empty bytes
    assert _decode_extra_json(b"") == {}


@pytest.mark.unit
def test_interaction_mode_property_rejects_non_string_values() -> None:
    """A misbehaving registry that puts a non-string under
    interaction_mode shouldn't poison downstream code paths that
    expect a string."""
    route = SelectedRoute(
        worker_url="x",
        eth_address="x",
        capability="x",
        offering="x",
        price_per_work_unit_wei=Decimal(0),
        work_unit="x",
        units_per_price=0,
        quote_id="x",
        quote_version=0,
        constraint_fingerprint=b"",
        route_fingerprint=b"",
        extra={"interaction_mode": 42},  # bad value
    )
    assert route.interaction_mode is None


@pytest.mark.unit
def test_large_price_string_decoded_intact() -> None:
    from livepeer.registry.v1 import resolver_pb2

    # Proto stores arbitrary-precision wei as a decimal big-int string.
    huge = str(2**200 - 1)
    proto = resolver_pb2.SelectedRoute(
        worker_url="x",
        eth_address="x",
        capability="x",
        offering="x",
        price_per_work_unit_wei=huge,
        work_unit="x",
        units_per_price=1,
        quote_id="x",
        quote_version=1,
        constraint_fingerprint=b"",
        route_fingerprint=b"",
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert int(dc.price_per_work_unit_wei) == 2**200 - 1
