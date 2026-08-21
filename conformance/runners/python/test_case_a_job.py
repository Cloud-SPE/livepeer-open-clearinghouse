"""Conformance: case (a) one-shot job.

Drives the Python SDK against the mock LOC + mock broker for
``scenarios/case_a_job.json`` and asserts:

  * SDK identity header sent on every LOC call.
  * Job result carries the broker body + the LOC settlement.
  * Telemetry batch is delivered to ``/v1/telemetry`` before close.
  * No body content (prompts / results) leaks into telemetry payloads.
"""

from __future__ import annotations

from typing import Any

import pytest


def _calls_to(loc: list[dict[str, Any]], path_prefix: str) -> list[dict[str, Any]]:
    return [c for c in loc if c["path"].startswith(path_prefix)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["case_a_job"], indirect=True)
async def test_case_a_job_end_to_end(sdk_client, call_logs) -> None:
    body = {"prompt": "this prompt MUST NOT leak into telemetry"}
    result = await sdk_client.submit_job(
        capability="cap.example",
        offering="off.example",
        estimated_units=10,
        body=body,
    )

    # The broker's response shape is propagated.
    assert result.actual_units == 7, "actual_units should come from the broker header"
    assert result.billed_value_wei == 7000
    assert result.refund_wei == 3000
    assert result.outcome == "OK"

    # Drain telemetry so it lands in the mock log.
    await sdk_client.aclose()

    loc, broker = call_logs()

    # SDK identity header is on every LOC call.
    for c in loc:
        assert c["headers"].get("livepeer-open-clearinghouse-sdk"), (
            f"{c['method']} {c['path']} missing SDK identity header"
        )
        assert c["headers"]["livepeer-open-clearinghouse-sdk"].startswith("python/"), (
            "SDK identity header should be `python/<semver>/<sha>`"
        )

    # Mint + settle hit LOC.
    assert _calls_to(loc, "/v1/jobs") and any(
        c["path"] == "/v1/jobs" and c["method"] == "POST" for c in loc
    ), "missing POST /v1/jobs"
    assert any(c["method"] == "POST" and "/settle" in c["path"] for c in loc), "missing settle call"

    # At least one telemetry batch was delivered before close.
    telemetry_calls = [c for c in loc if c["path"] == "/v1/telemetry"]
    assert telemetry_calls, "SDK must flush at least one telemetry batch"

    # Privacy invariant: NO request body content in telemetry payloads.
    for c in telemetry_calls:
        serialized = str(c["body"])
        assert "this prompt MUST NOT leak" not in serialized, (
            "prompt content leaked into telemetry — privacy invariant violation"
        )

    # Telemetry payload sanity: at least one request.* event present.
    saw_request_event = False
    for c in telemetry_calls:
        events = (c["body"] or {}).get("events", [])
        for ev in events:
            if str(ev.get("event_type", "")).startswith("request."):
                saw_request_event = True
    assert saw_request_event, "expected at least one request.* event in telemetry"

    # Broker received the mint envelope.
    assert any(c["path"] == "/v1/job" for c in broker), "broker should have been called"
