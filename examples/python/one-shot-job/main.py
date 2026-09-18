"""End-to-end example: submit a job via the handoff-mode SDK.

Run with:

    uv sync
    OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \\
    OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \\
    OPEN_CLEARINGHOUSE_OFFERING=gpt-oss-20b \\
    OPEN_CLEARINGHOUSE_MODEL=gpt-oss-20b \\
    uv run --package loc-example-one-shot-job python examples/python/one-shot-job/main.py

The SDK handles the full handoff dance for you: obtains a route-locked
spend authorization via POST /v1/jobs, calls the broker directly with
the authorization and caller proof, reads the broker's
Livepeer-Work-Units header from the response, and posts the settle
record back to LOC via POST /v1/jobs/{id}/settle.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid

from eth_hash.auto import keccak
from eth_keys.datatypes import PrivateKey
from livepeer_open_clearinghouse_sdk import (
    InsufficientCredit,
    NoRouteAvailable,
    OpenClearinghouseClient,
    OpenClearinghouseError,
    RateLimited,
    wei_to_eth,
)

# The caller key proves to the broker that this process is the one LOC
# authorized. It never leaves this process: the SDK only receives the public
# key and a signing callback. A fresh key per run is fine for an example;
# production callers keep their own.
CALLER_KEY = PrivateKey(os.urandom(32))
CALLER_PUBLIC_KEY = CALLER_KEY.public_key.to_compressed_bytes().hex()


def sign_caller_proof(authorization: bytes) -> str:
    """Livepeer-Caller-Proof: EIP-191 signature over the invocation digest."""
    digest = keccak(b"livepeer-invocation-proof/v1\x00" + authorization)
    signed = keccak(b"\x19Ethereum Signed Message:\n32" + digest)
    signature = bytearray(CALLER_KEY.sign_msg_hash(signed).to_bytes())
    signature[64] += 27  # R || S || V with V in {27, 28}
    return base64.b64encode(signature).decode()


async def chat(prompt: str) -> None:
    base_url = os.environ["OPEN_CLEARINGHOUSE_URL"]
    api_key = os.environ["OPEN_CLEARINGHOUSE_API_KEY"]
    offering = os.environ.get("OPEN_CLEARINGHOUSE_OFFERING", "gpt-oss-20b")
    # The OpenAI model name the offering advertises (its extra.openai.model);
    # brokers reject a chat request without one.
    model = os.environ.get("OPEN_CLEARINGHOUSE_MODEL", offering)

    async with OpenClearinghouseClient(base_url=base_url, api_key=api_key) as client:
        try:
            result = await client.submit_job(
                capability="openai:chat-completions",
                offering=offering,
                # Best-guess for input tokens; broker reports actual
                # consumption back via Livepeer-Work-Units.
                estimated_units=200,
                # Worst-case ceiling — LOC encumbers this much up
                # front. Refund happens at settle.
                max_total_units=2000,
                body={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                },
                request_id=str(uuid.uuid4()),
                caller_public_key=CALLER_PUBLIC_KEY,
                sign_caller_proof=sign_caller_proof,
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
