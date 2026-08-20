# RELIABILITY.md

The reliability principles for Livepeer Open Clearinghouse. Read this before changing
anything in `domains/billing`, `domains/payments`, or `domains/usage`.

## The principle

**Under-billing is a liability. Over-billing is recoverable.** Every design
choice in the payment path should favor failing closed over serving work.
An operator can refund a wrongly-charged user. An operator cannot easily
recover wei spent on a job that should have been refused.

## Fail-closed defaults

Return an error rather than serve work whenever:

- The user's credit balance cannot be safely decremented (DB write fails,
  optimistic-lock contention, balance read inconsistent).
- The spend-per-period cap would be exceeded after this charge.
- The user is not currently approved.
- The API key is revoked, expired, or unknown.
- The daemon returns any error from `Select` or `CreatePayment`.
- The user's `funded_value_wei` would exceed available balance.
- A usage record cannot be written idempotently.

For app-dev-facing failures, the canonical response is `402 Payment
Required` with a structured error body:

```json
{
  "error": {
    "code": "INSUFFICIENT_CREDIT",
    "message": "Available balance 12000 wei is less than required 50000 wei",
    "details": { "available_wei": "12000", "required_wei": "50000" }
  }
}
```

Other error codes: `SPEND_CAP_EXCEEDED`, `ACCOUNT_NOT_APPROVED`,
`API_KEY_REVOKED`, `DAEMON_UNAVAILABLE`, `RESERVATION_NOT_FOUND`,
`IDEMPOTENCY_KEY_REUSE`, `IDEMPOTENCY_IN_PROGRESS`, and
`IDEMPOTENCY_OUTCOME_UNKNOWN`.

## Idempotency

### Job and session creation

`POST /v1/jobs` and `POST /v1/sessions` require an `Idempotency-Key` header.
Keys are scoped by account and endpoint operation, not by API key, so changing
credentials does not weaken replay protection. LOC stores a canonical request
fingerprint; reusing a key with different content returns
`IDEMPOTENCY_KEY_REUSE`.

Before calling `CreatePayment`, LOC commits an `in_flight` claim with a stable
`request_id`. A completed response or deterministic failure is retained for 24
hours and replayed exactly without another mint or balance mutation. A concurrent
identical request returns `IDEMPOTENCY_IN_PROGRESS`.

The payer daemon does not yet accept a request idempotency key. Therefore an
in-flight claim whose payer outcome cannot be proven is marked `expired` after
60 seconds but is never reclaimed: subsequent retries return
`IDEMPOTENCY_OUTCOME_UNKNOWN`. This deliberately fails closed instead of risking
a second ticket. Once payer `CreatePayment` is idempotent, LOC can safely resolve
that crash window using the same stable `request_id`.

### Usage reports

`POST /v1/usage/report` is keyed on `(api_key_id, payment_work_id)`. A
duplicate report for the same `work_id` is a no-op (returns the first
report's reconciled state). The first report wins.

## State machines

### Payment record

```
                  ┌──────────────┐
                  │   reserved   │   created with funded_value
                  └──────┬───────┘
                         │ CreatePayment success
                         ▼
                  ┌──────────────┐
                  │    issued    │   payment_bytes returned to caller
                  └──────┬───────┘
                         │ usage report (request/response jobs only)
                         ▼
                  ┌──────────────┐
                  │  reconciled  │   delta refunded to balance
                  └──────────────┘

    from reserved on daemon error → ┌──────────────┐
                                    │   refused    │
                                    └──────────────┘
```

Transitions are atomic with the balance update. `refused` is terminal and
fully refunds the reservation.

### Credit balance changes

Every balance change is a row in `credit_ledger` with:
`(user_id, delta_wei, reason, related_payment_id?, related_topup_id?,
 created_at)`. The balance is computed as the running sum; a denormalized
`credit_balances.amount_wei` is kept for fast read but is always
reconcilable from the ledger.

## Spend-per-period cap

Each user has a per-period spend cap (default-configurable; per-user
override-able). A "period" is a wall-clock window (default 1 day, operator-
configurable).

- Charges in the current window accumulate in a `spend_window` row keyed
  `(user_id, window_start)`.
- A charge that would push `spent_in_window + delta > cap_wei` is rejected
  with `SPEND_CAP_EXCEEDED`.
- Auto-replenish (when balance hits zero) is itself subject to the cap:
  the top-up amount is `min(replenish_increment, cap_wei − spent_in_window)`.
- Window rollover is handled by an APScheduler job that runs on a
  fast cadence (every 60s by default) and computes the current window
  on read; we don't precompute rollovers.

## EV variance

Charging at issuance (`expected_value` from `CreatePayment`) means:

- For any single payment, Livepeer Open Clearinghouse charges the user the exact EV.
- For the pooled wallet, actual on-chain redemption is a random variable
  with mean `EV × N`.
- Over the long run, charged ≈ paid. Over a small window, they diverge.

Livepeer Open Clearinghouse does not observe on-chain redemption (MVP). The variance is
**absorbed by the operator's pooled wallet float**. There is no user-facing
exposure to this variance — the user is billed deterministically.

The operator-funded reserve in the pooled wallet must be sized to absorb
this variance plus settlement lag. As a rough rule for MVP: hold at least
`3 × max(daily_expected_payout, single_max_ticket_face_value)` of float
in the wallet.

## Concurrency

- Balance writes use Postgres row-level locking (`SELECT ... FOR UPDATE`)
  on the user's `credit_balances` row inside the same transaction as the
  `credit_ledger` insert and the `payments` upsert.
- Idempotency-key writes use `INSERT ... ON CONFLICT` to atomically claim
  a key.
- The single-instance assumption (one `livepeer-open-clearinghouse-gateway` process) means
  we don't need distributed locking. APScheduler runs in-process; no
  separate worker.

If/when we move to multi-instance, the spend-window check and the auto-
replenish job become the two hot spots that need attention.

## Daemon failure modes

### `service-registry-daemon.Select` returns an error

Return `503 SERVICE_UNAVAILABLE` to the caller. Do not charge. Do not record
a payment row.

### `payment-daemon.CreatePayment` returns an error

If the error is `INVALID_RECIPIENT_RAND` (session rotation), retry once
with a fresh `Select` call. If the second attempt also fails, return `503`
to the caller. Do not charge.

If the error is sender-validation-related (deposit zero, withdraw round
imminent), return `503 DAEMON_DEPOSIT_INSUFFICIENT` to the caller. This is
an operator-actionable failure — the pooled wallet needs deposit or
the withdrawal lock needs handling.

### Postgres unavailable

Return `503`. Livepeer Open Clearinghouse does not have an in-memory fallback. Postgres is a
hard dependency.

## Observability

For each ticket-mint call, emit a structured log line with:
`work_id`, `user_id`, `api_key_id`, `recipient`, `capability`, `offering`,
`funded_value_wei`, `expected_value_wei`, `result` (`issued|refused`),
`daemon_latency_ms`, `total_latency_ms`.

Prometheus metrics:
- `livepeer_open_clearinghouse_payments_total{result, capability}` counter
- `livepeer_open_clearinghouse_payment_latency_seconds{stage}` histogram (stages: `select`,
  `balance_check`, `create_payment`, `commit`)
- `livepeer_open_clearinghouse_balance_charged_wei_total{capability}` counter
- `livepeer_open_clearinghouse_credit_balance_wei{user_id}` gauge (cardinality concern;
  consider sampling/aggregating)
- `livepeer_open_clearinghouse_daemon_errors_total{daemon, kind}` counter

These are MVP-minimum. Full Victoria-stack integration is v2.
