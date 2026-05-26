"""Streaming session with WS topup (session-control-plus-media@v0).

Run with:

    uv sync
    OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \\
    OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \\
    uv run --package loc-example-streaming-ws python examples/python/streaming-ws/main.py

SessionRunner connects to the broker over a control WebSocket. When the
broker pushes a Livepeer-Balance-Low frame, the runner asks LOC for a
refill and delivers it back as a session.topup frame — the
on_refill_succeeded callback fires on each successful top-up.
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
            capability="livepeer:live-video-control",
            offering="session-control-plus-media",
            estimated_runway_units=1000,
            max_total_units=10000,
        )
        print(f"session opened: {handle.session_id} (mode={handle.mode})")

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
            # Hold the session briefly so the broker has a chance to push
            # at least one Livepeer-Balance-Low frame. Production code
            # would drive its own media plane on top of this WS.
            await asyncio.sleep(3)

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
