"""Extensible paid-session/v1 session with authoritative HTTP top-up.

Run with:

    uv sync
    OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \\
    OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \\
    uv run --package loc-example-streaming-http python examples/python/streaming-http/main.py

For HTTP-topup modes, the broker doesn't push balance-low frames over a
WebSocket — the customer's media plane observes balance-low out-of-band
and routes the signal into the runner. The runner then asks LOC for a
refill and POSTs it to the broker's control.topup_url.

The media plane passes the broker's normative balance object into the public
``on_balance`` method.
"""

from __future__ import annotations

import asyncio
import os

from livepeer_open_clearinghouse_sdk import (
    OpenClearinghouseClient,
    OpenClearinghouseError,
    SessionRunner,
)


async def main() -> None:
    base_url = os.environ["OPEN_CLEARINGHOUSE_URL"]
    api_key = os.environ["OPEN_CLEARINGHOUSE_API_KEY"]

    async with OpenClearinghouseClient(base_url=base_url, api_key=api_key) as client:
        handle = await client.open_session(
            capability="livepeer:remote-runner",
            offering="live-session-remote-runner",
            descriptor_schema="livepeer.session.remote-runner/v1",
            estimated_runway_units=1000,
            max_total_units=10000,
        )
        print(f"session opened: {handle.session_id} (protocol={handle.protocol})")

        async with SessionRunner(
            client=client,
            handle=handle,
            on_refill_succeeded=lambda e: print(
                f"refill #{e.refill_seq}: +{e.funded_value_wei} wei"
            ),
            on_refill_refused=lambda e: print(
                f"refill refused: {getattr(e.error, 'code', 'unknown')}"
            ),
            on_winddown_warning=lambda w: print(f"winddown: {w.reason}"),
        ) as runner:
            # Customer-driven refill. In production this fires when the
            # media plane observes balance-low on the runner channel.
            await runner.on_balance(
                {
                    "status": "low",
                    "claimed_units": 500,
                    "debited_units": 500,
                    "unit": "session_second",
                    "runway_units": 100,
                    "runway_seconds_estimate": 100,
                    "will_refuse_next_refill": False,
                }
            )

            settle = await runner.close(actual_units=750, outcome="complete")
            print("==== final settlement ====")
            print(f"outcome: {settle.get('outcome')}")
            print(f"billed:  {settle.get('billed_value_wei')} wei")
            print(f"refund:  {settle.get('refund_wei')} wei")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except OpenClearinghouseError as exc:
        print(f"loc error: {exc.code} - {exc}")
