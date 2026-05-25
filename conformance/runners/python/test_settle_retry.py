"""Conformance: settle retry on 5xx.

The mock always returns 503 for settle. The SDK's ``_post_with_retry``
attempts up to ``max_retries`` (3) before giving up. We assert >= 3
settle attempts in the call log.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["settle_retry"], indirect=True)
async def test_settle_retries_on_5xx(sdk_client, call_logs) -> None:
    # The job will complete because submit_job returns whatever the
    # settle response says (it doesn't raise on a final 503; LOC
    # janitor will reconcile via daemon GetSessionDebits). We just
    # care that the SDK retried before giving up.
    try:
        await sdk_client.submit_job(
            capability="cap.example",
            offering="off.example",
            estimated_units=10,
            body={"x": 1},
        )
    except Exception:
        # OK either way — some SDK builds raise on the final non-2xx,
        # others surface the unparseable response. The contract under
        # test is the retry count, not the final disposition.
        pass

    await sdk_client.aclose()
    loc, _broker = call_logs()
    settle_calls = [c for c in loc if "/settle" in c["path"]]
    assert len(settle_calls) >= 3, (
        f"SDK should retry settle at least 3 times on 5xx; got {len(settle_calls)}"
    )
