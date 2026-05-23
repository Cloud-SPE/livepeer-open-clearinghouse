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
(90% lines/statements/functions, 85% branches).

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

```ts
import { randomUUID } from "node:crypto";
import { OpenClearinghouseClient, InsufficientCredit } from "@livepeer/open-clearinghouse-sdk";

const ph = new OpenClearinghouseClient({
  baseUrl: "https://open-clearinghouse.example.com",
  apiKey: process.env.OPEN_CLEARINGHOUSE_API_KEY!,
});

const idem = randomUUID();
try {
  const mint = await ph.mintPayment({
    capability: "openai:chat-completions",
    offering: "vllm-qwen3.6-27b-default",
    workUnits: 1000,
    idempotencyKey: idem,
  });

  // ... POST to mint.recipient_eth_address's orch with header
  //     Livepeer-Payment: mint.payment_bytes ...

  await ph.reportUsage({
    paymentId: mint.payment_id,
    actualWorkUnits: 873,
    idempotencyKey: idem,
  });
} catch (err) {
  if (err instanceof InsufficientCredit) {
    console.error("need topup:", err.details);
  } else {
    throw err;
  }
}
```

Method surface (camelCase on the client; Livepeer Open Clearinghouse wire is snake_case):

|                                                                     |                                 |
| ------------------------------------------------------------------- | ------------------------------- |
| `listCapabilities()`                                                | discovery                       |
| `listOrchestrators({ capability })`                                 | discovery                       |
| `mintPayment({ capability, offering, workUnits, idempotencyKey? })` | the load-bearing call           |
| `reportUsage({ paymentId, actualWorkUnits, idempotencyKey? })`      | reconcile over-committed budget |

Errors are typed: `InsufficientCredit`, `SpendCapExceeded`,
`AccountNotApproved`, `EmailNotVerified`, `NoRouteAvailable`,
`RateLimited` (with `retryAfterSeconds`), `DuplicateRequest`,
`DaemonUnavailable`. Anything else falls through to the base
`OpenClearinghouseError`.
