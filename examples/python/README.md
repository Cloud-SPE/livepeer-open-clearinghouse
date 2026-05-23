# pymthouse-sdk (Python)

Reference async Python SDK for the PymtHouse gateway. ~150 lines of
real code; pulls in `httpx` and nothing else.

## Setup

```bash
uv sync --extra dev
```

## Run the tests

```bash
uv run pytest -q
```

Stubs every HTTP call via `respx`, so no live PymtHouse needed.

## Run the example against a live stack

```bash
PYMTHOUSE_URL=http://localhost:8000 \
PYMTHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
uv run python example.py
```

You'll see the mint response and a fake "would-send" line showing the
`Livepeer-Payment` header you'd pass to the orch.

## Use it from your app

```python
import asyncio
import uuid
from pymthouse_sdk import PymtHouseClient, InsufficientCredit

async def call_llm(prompt: str) -> str:
    async with PymtHouseClient(
        base_url="https://pymthouse.example.com",
        api_key="pymth_live_...",
    ) as ph:
        idem = str(uuid.uuid4())
        try:
            mint = await ph.mint_payment(
                capability="openai:chat-completions",
                offering="vllm-qwen3.6-27b-default",
                work_units=1000,
                idempotency_key=idem,
            )
        except InsufficientCredit as exc:
            raise RuntimeError(f"need topup: {exc.details}") from exc

        # ... POST to mint.recipient_eth_address's orch with
        # Livepeer-Payment: mint.payment_bytes ...

        # reconcile
        await ph.report_usage(
            payment_id=mint.payment_id,
            actual_work_units=873,
            idempotency_key=idem,
        )
        return "..."
```

Method surface:

| | |
|---|---|
| `list_capabilities()` | discovery via PymtHouse → service-registry-daemon |
| `list_orchestrators(capability=...)` | discovery |
| `mint_payment(capability, offering, work_units, idempotency_key=...)` | the load-bearing call |
| `report_usage(payment_id, actual_work_units, idempotency_key=...)` | reconcile over-committed budget |
| `.http` | escape hatch — direct httpx.AsyncClient |

Errors are typed: `InsufficientCredit`, `SpendCapExceeded`,
`AccountNotApproved`, `EmailNotVerified`, `NoRouteAvailable`,
`RateLimited` (with `retry_after_seconds`), `DuplicateRequest`,
`DaemonUnavailable`. Anything else falls through to the base
`PymtHouseError`.
