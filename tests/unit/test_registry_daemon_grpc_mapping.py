"""Unit tests for the registry-daemon dataclass <-> proto mapping."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from livepeer_open_clearinghouse import _gen  # noqa: F401  — pulls _gen onto sys.path
from livepeer_open_clearinghouse.providers.registry_daemon.client import (
    SelectedRoute,
    WorkUnitEstimator,
    _decode_extra_json,
    _selected_route_proto_to_dataclass,
)


def _route_proto(**overrides):  # type: ignore[no-untyped-def]
    from livepeer.registry.v1 import resolver_pb2

    values = {
        "worker_url": "https://orch.example/livepeer",
        "eth_address": "0x1234567890123456789012345678901234567890",
        "capability": "openai:chat-completions",
        "offering": "default",
        "price_per_work_unit_wei": "1000",
        "work_unit": "token",
        "units_per_price": 1,
        "quote_id": "q-abc",
        "quote_version": 1,
        "constraint_fingerprint": b"\x00" * 32,
        "route_fingerprint": b"\x11" * 32,
        "protocol": "paid-job/v1",
        "extra_json": json.dumps({"job": {"transports": ["unary"]}}).encode(),
    }
    values.update(overrides)
    return resolver_pb2.SelectedRoute(**values)


@pytest.mark.unit
def test_selected_route_proto_to_dataclass_carries_every_field() -> None:
    proto = _route_proto(
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
def test_selected_route_carries_signed_work_unit_estimator() -> None:
    proto = _route_proto(
        capability="openai:audio-transcriptions",
        work_unit="seconds",
        work_unit_estimator={
            "id": "multipart-audio-duration/v1",
            "rounding": "ceil-to-whole-seconds",
            "exactness": "exact-or-reject",
            "fixtures": (
                "livepeer-network-protocol/extractors/fixtures/multipart-audio-duration-v1"
            ),
        },
    )

    route = _selected_route_proto_to_dataclass(proto)

    assert route.work_unit_estimator == WorkUnitEstimator(
        id="multipart-audio-duration/v1",
        rounding="ceil-to-whole-seconds",
        exactness="exact-or-reject",
        package=None,
        fixtures=("livepeer-network-protocol/extractors/fixtures/multipart-audio-duration-v1"),
    )
    assert route.snapshot()["work_unit_estimator"] == {
        "id": "multipart-audio-duration/v1",
        "rounding": "ceil-to-whole-seconds",
        "exactness": "exact-or-reject",
        "package": None,
        "fixtures": ("livepeer-network-protocol/extractors/fixtures/multipart-audio-duration-v1"),
    }


@pytest.mark.unit
def test_absent_work_unit_estimator_remains_absent() -> None:
    route = _selected_route_proto_to_dataclass(_route_proto())
    assert route.work_unit_estimator is None


@pytest.mark.unit
def test_selected_route_preserves_overlapping_settlement_keys_in_snapshot() -> None:
    newer = {
        "public_key": "0x" + "04" + "11" * 64,
        "not_before": "2026-08-20T12:00:00Z",
        "expires_at": "2026-08-21T12:00:00Z",
        "introduced_in_publication_seq": 8,
    }
    outgoing = {
        "public_key": "0x" + "04" + "22" * 64,
        "not_before": "2026-08-19T12:00:00Z",
        "expires_at": "2026-08-20T18:00:00Z",
        "introduced_in_publication_seq": 7,
    }

    route = _selected_route_proto_to_dataclass(_route_proto(settlement_keys=[newer, outgoing]))

    assert [key.public_key for key in route.settlement_keys] == [
        newer["public_key"],
        outgoing["public_key"],
    ]
    assert route.snapshot()["settlement_keys"] == [
        {**newer, "introduced_in_publication_seq": "8"},
        {**outgoing, "introduced_in_publication_seq": "7"},
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        {
            "public_key": "0x1234",
            "not_before": "2026-08-20T12:00:00Z",
            "expires_at": "2026-08-21T12:00:00Z",
        },
        {
            "public_key": "0x" + "04" + "11" * 64,
            "not_before": "not-a-time",
            "expires_at": "2026-08-21T12:00:00Z",
        },
        {
            "public_key": "0x" + "04" + "11" * 64,
            "not_before": "2026-08-21T12:00:00Z",
            "expires_at": "2026-08-20T12:00:00Z",
        },
    ],
)
def test_malformed_settlement_key_fails_at_boundary(key: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _selected_route_proto_to_dataclass(_route_proto(settlement_keys=[key]))


@pytest.mark.unit
def test_zero_denominator_fails_at_boundary() -> None:
    proto = _route_proto(
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
    with pytest.raises(ValidationError):
        _selected_route_proto_to_dataclass(proto)


@pytest.mark.unit
def test_extra_json_decoded_into_typed_session_axes() -> None:
    proto = _route_proto(
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
        protocol="paid-session/v1",
        extra_json=json.dumps(
            {
                "session": {
                    "descriptor_schema": "openai-realtime/v1",
                    "metering": "runner-reported",
                    "attachment": "external",
                    "refill": "bounded",
                    "future_axis": True,
                },
                "category": "audio",
            }
        ).encode(),
    )
    dc = _selected_route_proto_to_dataclass(proto)
    assert dc.protocol == "paid-session/v1"
    assert dc.session is not None
    assert dc.session.descriptor_schema == "openai-realtime/v1"
    assert dc.session.refill == "bounded"
    assert dc.session.model_extra == {"future_axis": True}


@pytest.mark.unit
def test_missing_axes_fail_at_boundary() -> None:
    proto = _route_proto(
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
    with pytest.raises(ValidationError):
        _selected_route_proto_to_dataclass(proto)


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
def test_unknown_or_v0_protocol_fails_at_boundary() -> None:
    with pytest.raises(ValidationError):
        SelectedRoute(
            worker_url="x",
            eth_address="x",
            capability="x",
            offering="x",
            price_per_work_unit_wei=Decimal(0),
            work_unit="x",
            units_per_price=1,
            quote_id="x",
            quote_version=0,
            constraint_fingerprint=b"",
            route_fingerprint=b"",
            protocol="not-a-protocol",  # type: ignore[arg-type]
            extra={"job": {"transports": ["stream"]}},
        )


@pytest.mark.unit
def test_large_price_string_decoded_intact() -> None:
    # Proto stores arbitrary-precision wei as a decimal big-int string.
    huge = str(2**200 - 1)
    proto = _route_proto(
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
