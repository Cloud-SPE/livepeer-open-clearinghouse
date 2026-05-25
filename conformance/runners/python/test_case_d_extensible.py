"""Conformance: case (d-extensible) — long-running session that auto-refills.

Drives the Python SDK to:

  1. ``open_session`` — LOC mints + SDK opens the broker session.
  2. ``refill_session`` — manually triggers a refill (the real auto-
     refill is event-driven via ``on_balance_low``; we exercise the
     same wire path explicitly so the conformance run is deterministic).
  3. ``close_session`` — settles and tears down.

Asserts the LOC + broker call log matches the expected sequence and
that the SDK identity header is present on every LOC call.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["case_d_extensible_session"], indirect=True)
async def test_case_d_extensible_open_refill_close(sdk_client, call_logs) -> None:
    handle = await sdk_client.open_session(
        capability="cap.live",
        offering="off.live",
        estimated_runway_units=100,
        max_total_units=500,
    )
    assert handle.session_id is not None

    refill_result = await sdk_client.refill_session(
        session_id=handle.session_id,
        observed_consumed_units=40,
    )
    assert refill_result is not None
    assert refill_result["payment_envelope"] == "REFILL-ENV"

    close_result = await sdk_client.close_session(
        session_id=handle.session_id,
        actual_units=60,
    )
    assert close_result["outcome"] == "OK"
    assert close_result["actual_units"] == 60
    assert close_result["refund_wei"] == 190000

    await sdk_client.aclose()

    loc, broker = call_logs()
    paths = [(c["method"], c["path"]) for c in loc]
    assert ("POST", "/v1/sessions") in paths, "open_session should call POST /v1/sessions"
    assert any(
        m == "POST" and "/refill" in p for m, p in paths
    ), "refill_session should call /refill"
    assert any(
        m == "POST" and "/close" in p for m, p in paths
    ), "close_session should call /close"

    # SDK identity header on every LOC call.
    for c in loc:
        assert c["headers"].get("livepeer-open-clearinghouse-sdk"), (
            f"missing identity header on {c['method']} {c['path']}"
        )

    # Telemetry was delivered.
    assert any(c["path"] == "/v1/telemetry" for c in loc), "no telemetry batches delivered"

    # The raw ``open_session`` path returns the handle without
    # contacting the broker — that's the SessionRunner's job for
    # (d-extensible) modes. A dedicated SessionRunner conformance
    # scenario would assert the broker handshake separately; here
    # we record that no broker traffic is expected from open alone.
    cap_calls = [c for c in broker if c["path"] == "/v1/cap"]
    assert cap_calls == [], (
        "raw open_session/refill/close should not contact the broker; "
        f"got {len(cap_calls)} /v1/cap calls"
    )
