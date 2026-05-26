"""End-to-end example: submit a job via the handoff-mode SDK.

Run with:

    uv sync --extra dev
    OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \\
    OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \\
    uv run python example.py

The SDK handles the full handoff dance for you: opens a job via
POST /v1/jobs (which mints a payment envelope), calls the broker
directly with the envelope as Livepeer-Payment, reads the broker's
Livepeer-Work-Units header from the response, and posts the settle
record back to LOC via POST /v1/jobs/{id}/settle.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from livepeer_open_clearinghouse_sdk import (
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseClient,
    OpenClearinghouseError,
    RateLimited,
    wei_to_eth,
)


async def chat(prompt: str) -> None:
    base_url = os.environ["OPEN_CLEARINGHOUSE_URL"]
    api_key = os.environ["OPEN_CLEARINGHOUSE_API_KEY"]

    async with OpenClearinghouseClient(base_url=base_url, api_key=api_key) as client:
        try:
            result = await client.submit_job(
                capability="openai:chat-completions",
                offering="gpt-oss-20b",
                # Best-guess for input tokens; broker reports actual
                # consumption back via Livepeer-Work-Units.
                estimated_units=200,
                # Worst-case ceiling — LOC encumbers this much up
                # front. Refund happens at settle.
                max_total_units=2000,
                body={
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                },
                request_id=str(uuid.uuid4()),
            )
        except InsufficientCredit as exc:
            print(f"not enough credit: {exc.details}")
            return
        except NoRouteAvailable:
            print("no orchestrator advertising this capability/offering")
            return
        except RateLimited as exc:
            print(f"rate limited; retry after {exc.retry_after_seconds}s")
            return
        except OpenClearinghouseError as exc:
            print(f"loc error: {exc.code} - {exc}")
            return

    # Application output
    if result.status == 200:
        print("==== broker response ====")
        print(result.body)
        print()
        print("==== final accounting ====")
        print(f"actual units consumed: {result.actual_units}")
        print(f"billed:                {wei_to_eth(result.billed_value_wei):.10f} ETH")
        print(f"refund:                {wei_to_eth(result.refund_wei):.10f} ETH")
        print(f"outcome:               {result.outcome}")
        if result.cap_status.will_refuse_next_refill:
            print(
                "⚠️  cap warning:",
                result.cap_status.winddown_reason,
                "— another job at this size may be refused",
            )
    else:
        print(f"broker returned {result.status}")
        print(result.body)


if __name__ == "__main__":
    asyncio.run(chat("explain handoff mode in two sentences"))
