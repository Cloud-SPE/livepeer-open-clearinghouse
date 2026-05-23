# PymtHouse — SDK examples

Reference clients for the gateway, one per major language. Each is a
self-contained project — independent build, dependencies, and tests —
so an app developer can drop the directory wherever and use it.

These are **reference implementations, not packaged SDKs.** Treat them
as starting points: the API surface is small enough (four methods) that
embedding a copy is the simplest distribution.

## Languages

| Language | Path | Toolchain | Test runner |
|---|---|---|---|
| Python | [`python/`](./python) | uv | pytest + respx |
| TypeScript | [`typescript/`](./typescript) | pnpm | vitest |
| Go | [`go/`](./go) | `go` (1.22+) | std `testing` + httptest |
| Rust | [`rust/`](./rust) | cargo (1.75+) | std `test` + wiremock |

Each subdirectory has a `README.md` with setup, test, and example
commands.

## API surface (consistent across all four)

| Method | Wire endpoint |
|---|---|
| `list_capabilities()` | `GET /v1/capabilities` |
| `list_orchestrators(capability?)` | `GET /v1/orchestrators` |
| `mint_payment(capability, offering, work_units, idempotency_key?)` | `POST /v1/payments/mint` |
| `report_usage(payment_id, actual_work_units, idempotency_key?)` | `POST /v1/usage/report` |

Method names follow each language's idiomatic style (`snake_case` in
Python/Rust/Go, `camelCase` in TypeScript) but map to the same wire
shape.

## Error shape (consistent across all four)

PymtHouse returns errors as:

```json
{ "error": { "code": "...", "message": "...", "details": { ... } } }
```

Each SDK maps these into typed errors. The kinds you'll see:

| Code | Meaning |
|---|---|
| `INSUFFICIENT_CREDIT` | Top up or wait for auto-replenish |
| `SPEND_CAP_EXCEEDED` | Per-period cap reached |
| `account_not_approved` | Operator hasn't approved you yet |
| `email_not_verified` | Verification email pending |
| `NO_ROUTE_AVAILABLE` | No orch advertising this capability+offering |
| `rate_limited` | Back off; `Retry-After` header tells you how long |
| `DUPLICATE_REQUEST` | Same `Idempotency-Key` reused with different inputs |
| `DAEMON_UNAVAILABLE` | PymtHouse can't reach payment-daemon |

## The integration shape (for your reference)

```
                 ┌────────────────┐
your-app-server  │   PymtHouse    │   orch (real Livepeer
                 │   gateway      │    orchestrator)
─────────────────►                │
1. POST /v1/payments/mint
   { capability, offering, work_units }
   X-API-Key: pymth_live_...
   Idempotency-Key: <uuid>
                 │ returns        │
                 │ payment_bytes  │
◄─────────────────                │
                 │                │
                 └────────────────┘

──────────────────────────────────────────►
2. POST {orch_url}/your-endpoint
   Livepeer-Payment: <payment_bytes>
   (your normal request body)
                                       returns inference result
◄──────────────────────────────────────────

(optional — for request/response APIs where you over-committed budget)
─────────────────►
3. POST /v1/usage/report
   { payment_id, actual_work_units }
   Idempotency-Key: <same uuid as step 1>
```

The two HTTP round-trips (mint, then orch) are the load-bearing path.
The third is a reconciliation refund — skip it for streaming APIs where
the orch consumes as it goes.

## What you don't have to do

- **Talk to the blockchain.** PymtHouse + payment-daemon handle it.
- **Manage a wallet.** The pooled signing wallet is the operator's.
- **Verify tickets.** The orch does that against PymtHouse's signer key.
- **Listen for ticket-win events.** PymtHouse charges Expected Value
  at issuance (probabilistic micropayment math) and absorbs short-term
  variance against the pool — Option A in the design docs.

## Smoke-test all four at once

```bash
cd examples/python && uv sync --extra dev && uv run pytest -q && cd -
cd examples/typescript && pnpm install && pnpm test && pnpm build && cd -
cd examples/go && go test ./... && cd -
cd examples/rust && cargo test && cd -
```

All four should pass without a running gateway — every SDK stubs its
HTTP layer in tests.
