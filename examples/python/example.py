"""End-to-end example: mint a payment, send it to an orch, reconcile usage.

Run with:

    uv sync --extra dev
    PYMTHOUSE_URL=http://localhost:8000 \\
    PYMTHOUSE_API_KEY=pymth_live_... \\
    uv run python example.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from pymthouse_sdk import (
    InsufficientCredit,
    NoRouteAvailable,
    PymtHouseClient,
    RateLimited,
)


async def chat(prompt: str) -> None:
    base_url = os.environ["PYMTHOUSE_URL"]
    api_key = os.environ["PYMTHOUSE_API_KEY"]

    async with PymtHouseClient(base_url=base_url, api_key=api_key) as ph:
        # 1. Pick an offering. Pin to a specific one in prod; here we
        #    just take the first chat-completions route we find.
        caps = await ph.list_capabilities()
        chat_cap = next(c for c in caps if c["name"] == "openai:chat-completions")
        offering = chat_cap["offerings"][0]["id"]
        print(f"using offering: {offering}")

        # 2. Commit a budget of ~1000 tokens. Over-commit slightly; we
        #    reconcile the real number at the end via report_usage.
        budget = 1000
        idem = str(uuid.uuid4())  # one per logical request
        try:
            mint = await ph.mint_payment(
                capability="openai:chat-completions",
                offering=offering,
                work_units=budget,
                idempotency_key=idem,
            )
        except InsufficientCredit as exc:
            print(f"need topup: {exc.details}")
            return
        except NoRouteAvailable:
            print("no orch advertising this offering — try another")
            return
        except RateLimited as exc:
            print(f"rate limited; retry in {exc.retry_after_seconds}s")
            return
        print(f"minted: work_id={mint.work_id[:16]}… ev={mint.expected_value_wei}")

        # 3. Look up the orch URL — your discovery layer / cache should
        #    have this. For the example we'll skip the orch call and just
        #    show the header shape you'd send:
        print(f"orch={mint.recipient_eth_address}")
        print(f"Livepeer-Payment header value (truncated): {mint.payment_bytes[:48]}…")

        # The real call would be:
        # async with httpx.AsyncClient() as orch:
        #     r = await orch.post(
        #         orch_url + "/v1/chat/completions",
        #         headers={"Livepeer-Payment": mint.payment_bytes},
        #         json={"model": offering, "messages": [{"role": "user", "content": prompt}]},
        #         timeout=120.0,
        #     )
        #     reply = r.json()
        #     actual_tokens = reply["usage"]["total_tokens"]
        actual_tokens = 873  # pretend the orch said this

        # 4. Reconcile. Same Idempotency-Key per logical request.
        result = await ph.report_usage(
            payment_id=mint.payment_id,
            actual_work_units=actual_tokens,
            idempotency_key=idem,
        )
        print(f"refunded {result['refunded_wei']} wei; new balance {result['new_balance_wei']} wei")


if __name__ == "__main__":
    asyncio.run(chat("Hello, world."))
