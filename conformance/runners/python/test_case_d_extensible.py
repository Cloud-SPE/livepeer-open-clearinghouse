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
from livepeer_open_clearinghouse_sdk.session_runner import SessionBalance, SessionRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["case_d_extensible_session"], indirect=True)
async def test_case_d_extensible_open_refill_close(sdk_client, call_logs) -> None:
    handle = await sdk_client.open_session(
        capability="cap.live",
        offering="off.live",
        descriptor_schema="livepeer-session-test/v1",
        estimated_runway_units=100,
        max_total_units=500,
    )
    assert handle.session_id is not None

    runner = SessionRunner(client=sdk_client, handle=handle)
    broker_session = await runner.start()
    assert broker_session.runtime_schema == "livepeer-session-test/v1"
    assert (await runner.status())["state"] == "active"
    await runner.on_balance(
        SessionBalance(
            status="low",
            claimed_units=40,
            debited_units=40,
            unit="participant_minutes",
            runway_units=10,
            runway_seconds_estimate=600,
            will_refuse_next_refill=False,
        )
    )
    close_result = await runner.close(actual_units=60)
    assert close_result["outcome"] == "OK"
    assert close_result["actual_units"] == 60
    assert close_result["refund_wei"] == 190000

    await sdk_client.aclose()

    loc, broker = call_logs()
    paths = [(c["method"], c["path"]) for c in loc]
    assert ("POST", "/v1/sessions") in paths, "open_session should call POST /v1/sessions"
    assert any(m == "POST" and "/refill" in p for m, p in paths), (
        "refill_session should call /refill"
    )
    assert any(m == "POST" and "/close" in p for m, p in paths), "close_session should call /close"

    # SDK identity header on every LOC call.
    for c in loc:
        assert c["headers"].get("livepeer-open-clearinghouse-sdk"), (
            f"missing identity header on {c['method']} {c['path']}"
        )

    # Telemetry was delivered.
    assert any(c["path"] == "/v1/telemetry" for c in loc), "no telemetry batches delivered"

    broker_paths = [(c["method"], c["path"]) for c in broker]
    assert ("POST", "/v1/session") in broker_paths
    assert ("GET", "/v1/session/broker-session") in broker_paths
    assert ("POST", "/v1/session/broker-session/topup") in broker_paths
    assert ("POST", "/v1/session/broker-session/end") in broker_paths
