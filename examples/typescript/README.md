# @pymthouse/sdk (TypeScript)

Reference TypeScript SDK for the PymtHouse gateway. Zero runtime
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

Stubs `fetch` per test; no live PymtHouse needed.

## Type-check

```bash
pnpm build
```

(`build` is `tsc --noEmit` — we don't ship a compiled artifact in this
reference; embed the `src/` directly or compile in your app.)

## Run the example against a live stack

```bash
PYMTHOUSE_URL=http://localhost:8000 \
PYMTHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
pnpm example
```

## Use it from your app

```ts
import { randomUUID } from "node:crypto";
import { PymtHouseClient, InsufficientCredit } from "@pymthouse/sdk";

const ph = new PymtHouseClient({
  baseUrl: "https://pymthouse.example.com",
  apiKey: process.env.PYMTHOUSE_API_KEY!,
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

Method surface (camelCase on the client; PymtHouse wire is snake_case):

| | |
|---|---|
| `listCapabilities()` | discovery |
| `listOrchestrators({ capability })` | discovery |
| `mintPayment({ capability, offering, workUnits, idempotencyKey? })` | the load-bearing call |
| `reportUsage({ paymentId, actualWorkUnits, idempotencyKey? })` | reconcile over-committed budget |

Errors are typed: `InsufficientCredit`, `SpendCapExceeded`,
`AccountNotApproved`, `EmailNotVerified`, `NoRouteAvailable`,
`RateLimited` (with `retryAfterSeconds`), `DuplicateRequest`,
`DaemonUnavailable`. Anything else falls through to the base
`PymtHouseError`.
