# Livepeer Open Clearinghouse — SDKs

Reference client SDKs for the gateway, one per major language. Each is
a published, self-contained package — independent build, dependencies,
and tests — that an app can depend on directly.

| Language | Path | Package | Toolchain | Test runner | Lint | Coverage |
|---|---|---|---|---|---|---|
| Python | [`python/`](./python) | `livepeer-open-clearinghouse-sdk` (PyPI) | uv | pytest + respx | ruff (E/F/I/B/UP/RUF/SIM/ASYNC) + ruff format | pytest-cov, ≥90% gate |
| TypeScript | [`typescript/`](./typescript) | `@livepeer/open-clearinghouse-sdk` (npm) | pnpm | vitest | ESLint (strict-type-checked + stylistic-type-checked) + Prettier | vitest v8, ≥90% gate |
| Go | [`go/`](./go) | `github.com/livepeer/livepeer-open-clearinghouse-sdk-go` | `go` (1.22+) | std `testing` + httptest | golangci-lint (errcheck, govet, staticcheck, unused, gosec, revive, gocritic, bodyclose, misspell) + gofmt | `go test -coverprofile` |
| Rust | [`rust/`](./rust) | `livepeer-open-clearinghouse-sdk` (crates.io) | cargo (1.75+) | std `test` + wiremock | `cargo clippy -D warnings` with `pedantic + nursery` | `cargo llvm-cov` |

Each subdirectory has a `README.md` with setup, test, and lint
commands. Usage examples live in
`../examples/<lang>/{one-shot-job,streaming-ws,streaming-http}/`.

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

Livepeer Open Clearinghouse returns errors as:

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
| `DAEMON_UNAVAILABLE` | Livepeer Open Clearinghouse can't reach payment-daemon |

## The integration shape (for your reference)

```
                 ┌────────────────┐
your-app-server  │   Livepeer Open Clearinghouse    │   orch (real Livepeer
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

- **Talk to the blockchain.** Livepeer Open Clearinghouse + payment-daemon handle it.
- **Manage a wallet.** The pooled signing wallet is the operator's.
- **Verify tickets.** The orch does that against Livepeer Open Clearinghouse's signer key.
- **Listen for ticket-win events.** Livepeer Open Clearinghouse charges Expected Value
  at issuance (probabilistic micropayment math) and absorbs short-term
  variance against the pool — Option A in the design docs.

## Smoke-test all four at once

```bash
# Tests (run from repo root)
( cd sdks/python     && uv sync && uv run pytest -q )
( cd sdks/typescript && pnpm install && pnpm test && pnpm build )
( cd sdks/go         && go test ./livepeer_open_clearinghouse/... )
( cd sdks/rust       && cargo test )
```

All four should pass without a running gateway — every SDK stubs its
HTTP layer in tests.

## Lint everything

```bash
( cd sdks/python     && uv run ruff check . && uv run ruff format --check . )
( cd sdks/typescript && pnpm lint )
( cd sdks/go         && golangci-lint run ./... )
( cd sdks/rust       && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check )
```

## Coverage reports

```bash
( cd sdks/python     && uv run pytest -q )           # term + html in .coverage_html
( cd sdks/typescript && pnpm test:coverage )         # term + html in coverage/
( cd sdks/go         && go test ./livepeer_open_clearinghouse/... -coverprofile=cover.out && go tool cover -func=cover.out )
( cd sdks/rust       && cargo llvm-cov --html )      # html in target/llvm-cov/html
```

Current coverage (one snapshot):

| SDK | Statements / Lines |
|---|---|
| Python | 96.4% lines, 12 branches missed of 12 evaluated |
| TypeScript | 100% statements, 96.5% branches |
| Go (livepeer_open_clearinghouse package) | 89.7% statements |
| Rust | 97.9% lines, 95.3% regions |
