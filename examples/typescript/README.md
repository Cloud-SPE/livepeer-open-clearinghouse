# @livepeer/open-clearinghouse-sdk (TypeScript)

Reference TypeScript SDK for the Livepeer Open Clearinghouse gateway. Zero runtime
dependencies — built on `fetch` (Node 20+ has it native) — plus dev-time
`tsx` + `vitest` + `typescript`.

## Setup

```bash
pnpm install
```

## Run the tests

```bash
pnpm test
```

Stubs `fetch` per test; no live Livepeer Open Clearinghouse needed.

## Coverage

```bash
pnpm test:coverage
```

v8-based; HTML in `coverage/`. Thresholds enforced in `vitest.config.ts`
(at least the cross-SDK 75% floor; actual measured coverage is
higher). See `docs/exec-plans/active/002-long-running-sessions.md`
§"SDK conformance criteria for telemetry" for why the floor is set
where it is.

## Lint + format

```bash
pnpm lint          # eslint + prettier --check
pnpm lint:fix      # eslint --fix + prettier --write
```

Rules: ESLint flat config with `@typescript-eslint`'s
`strict-type-checked` + `stylistic-type-checked` presets. Prettier
handles formatting. Both ignore `*.config.{ts,js}` so the project
parser doesn't choke on its own configs.

## Type-check

```bash
pnpm build
```

(`build` is `tsc --noEmit` — we don't ship a compiled artifact in this
reference; embed the `src/` directly or compile in your app.)

## Run the example against a live stack

```bash
OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
OPEN_CLEARINGHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
pnpm example
```

## Use it from your app

Livepeer Open Clearinghouse runs in **handoff mode**: LOC mints the
payment envelope; the SDK calls the broker directly with that
envelope; LOC settles based on the broker's reported work units.

```ts
import { OpenClearinghouseClient } from "@livepeer/open-clearinghouse-sdk";

const ph = new OpenClearinghouseClient({
  baseUrl: "https://open-clearinghouse.example.com",
  apiKey: process.env.OPEN_CLEARINGHOUSE_API_KEY!,
});

const result = await ph.submitJob({
  capability: "openai:chat-completions",
  offering: "vllm-qwen3.6-27b-default",
  estimatedUnits: 200,
  maxTotalUnits: 2000,
  body: {
    messages: [{ role: "user", content: "hello" }],
    max_tokens: 50,
  },
});

// result.body is the broker's response (parsed JSON or raw text)
// result.actualUnits / billedValueWei / refundWei / outcome carry the
// LOC settlement summary.
console.log(result.outcome, result.actualUnits, "units billed");
```

Long-running session shape:

```ts
const handle = await ph.openSession({
  capability: "cap.live",
  offering: "off.live",
  estimatedRunwayUnits: 1000,
  maxTotalUnits: 10_000,
});
// ... stream work against handle.brokerUrl, refill via SessionRunner ...
await ph.closeSession({ sessionId: handle.sessionId, actualUnits: 4250 });
```

Method surface (camelCase on the client; LOC wire is snake_case):

|                                                                                                                       |                                  |
| --------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| `listCapabilities()`                                                                                                  | discovery                        |
| `listOrchestrators({ capability })`                                                                                   | discovery                        |
| `submitJob({ capability, offering, estimatedUnits, body, maxTotalUnits? })`                                           | one-shot job (cases a/b/c)       |
| `openSession({ capability, offering, estimatedRunwayUnits, maxTotalUnits })`                                          | open long-running session (case d) |
| `refillSession(sessionId, { observedConsumedUnits })`                                                                 | top up an open session           |
| `closeSession({ sessionId, actualUnits })`                                                                            | settle + close a session         |
| `telemetry`                                                                                                           | direct access to the (mandatory) `TelemetryEmitter` |

The `Livepeer-Open-Clearinghouse-SDK` identity header is sent on every
call, and telemetry events (`request.mintStarted`,
`request.settleCompleted`, `session.opened`, …) fire fire-and-forget
through `/v1/telemetry`. There is no telemetry opt-out.

Errors are typed: `InsufficientCredit`, `SpendCapExceeded`,
`AccountNotApproved`, `EmailNotVerified`, `NoRouteAvailable`,
`RateLimited` (with `retryAfterSeconds`), `DuplicateRequest`,
`DaemonUnavailable`. Anything else falls through to the base
`OpenClearinghouseError`.
