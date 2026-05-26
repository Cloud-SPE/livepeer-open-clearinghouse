# livepeer-open-clearinghouse-sdk (Python)

Reference async Python SDK for the Livepeer Open Clearinghouse gateway. ~150 lines of
real code; pulls in `httpx` and nothing else.

## Setup

```bash
uv sync --extra dev
```

## Run the tests

```bash
uv run pytest -q
```

Stubs every HTTP call via `respx`, so no live Livepeer Open Clearinghouse needed.
Coverage is auto-collected (HTML in `.coverage_html/`); the suite
fails at < 75% (matches the cross-SDK conformance baseline; see
`docs/exec-plans/active/002-long-running-sessions.md`
§"SDK conformance criteria for telemetry"). Actual measured coverage
is ~90% — the floor is set lower because cross-language edge cases
are covered by the conformance harness rather than per-SDK tests.

## Lint + format

```bash
uv run ruff check .             # lint
uv run ruff format --check .    # format check
uv run ruff check --fix .       # auto-fix
uv run ruff format .            # auto-format
```

Rule set: `E,F,I,B,UP,RUF,SIM,ASYNC`. Configured in `pyproject.toml`
under `[tool.ruff.lint]`.

## Run the example against a live stack

```bash
OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
OPEN_CLEARINGHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
uv run python example.py
```

`example.py` runs a one-shot job through `submit_job` — you'll see
the broker response body + the LOC settlement (`actual_units`,
`billed_value_wei`, `refund_wei`, `outcome`).

## Use it from your app

Livepeer Open Clearinghouse runs in **handoff mode**: LOC mints the
payment envelope; the SDK calls the broker directly with that
envelope; LOC settles based on the broker's reported work units.
Three primary entry points cover all interaction shapes:

  - `submit_job(...)` — case (a)/(b)/(c): one-shot mint → broker
    call → settle. Best for request/response workloads (OpenAI-shaped
    LLM calls, transcoding, etc.).
  - `open_session(...)` — case (d): long-running session. Returns a
    `SessionHandle` carrying the broker URL + initial envelope; pair
    with `SessionRunner` for the automatic refill loop, or drive
    `refill_session` / `close_session` manually.

```python
import asyncio
from livepeer_open_clearinghouse_sdk import OpenClearinghouseClient

async def call_llm(prompt: str) -> str:
    async with OpenClearinghouseClient(
        base_url="https://open-clearinghouse.example.com",
        api_key="pymth_live_...",
    ) as ph:
        result = await ph.submit_job(
            capability="openai:chat-completions",
            offering="vllm-qwen3.6-27b-default",
            estimated_units=200,
            max_total_units=2000,
            body={
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 50,
            },
        )
        # result.body is the broker's response (parsed JSON or raw text)
        # result.actual_units / billed_value_wei / refund_wei / outcome
        # carry the LOC settlement.
        return str(result.body)
```

Long-running session shape:

```python
async with OpenClearinghouseClient(...) as ph:
    handle = await ph.open_session(
        capability="cap.live",
        offering="off.live",
        estimated_runway_units=1000,
        max_total_units=10_000,
    )
    # ... stream work against handle.broker_url, refill via SessionRunner ...
    closed = await ph.close_session(
        session_id=handle.session_id, actual_units=...
    )
```

Method surface:

| | |
|---|---|
| `list_capabilities()` | discovery via LOC → service-registry-daemon |
| `list_orchestrators(capability=...)` | discovery |
| `submit_job(capability, offering, estimated_units, body, max_total_units=...)` | one-shot job (cases a/b/c) |
| `open_session(capability, offering, estimated_runway_units, max_total_units)` | open long-running session (case d) |
| `refill_session(session_id, observed_consumed_units)` | top up an open session |
| `close_session(session_id, actual_units)` | settle + close a session |
| `telemetry` | direct access to the (mandatory) `TelemetryEmitter` |
| `.http` | escape hatch — direct `httpx.AsyncClient` |

The `Livepeer-Open-Clearinghouse-SDK` identity header is sent on every
call, and telemetry events (`request.mint_started`,
`request.settle_completed`, `session.opened`, …) fire fire-and-forget
through `/v1/telemetry`. There is no telemetry opt-out.

Errors are typed: `InsufficientCredit`, `SpendCapExceeded`,
`AccountNotApproved`, `EmailNotVerified`, `NoRouteAvailable`,
`RateLimited` (with `retry_after_seconds`), `DuplicateRequest`,
`DaemonUnavailable`. Anything else falls through to the base
`OpenClearinghouseError`.
