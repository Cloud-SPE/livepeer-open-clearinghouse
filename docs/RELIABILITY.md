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

### Job creation, session creation, and session refills

`POST /v1/jobs`, `POST /v1/sessions`, and
`POST /v1/sessions/{id}/refill` require an `Idempotency-Key` header.
Keys are scoped by account and endpoint operation, not by API key, so changing
credentials does not weaken replay protection. LOC stores a canonical request
fingerprint; reusing a key with different content returns
`IDEMPOTENCY_KEY_REUSE`.

Before calling `CreatePayment`, LOC commits an `in_flight` claim with a stable
`request_id`. A completed response or deterministic failure is retained for 24
hours and replayed exactly without another mint or balance mutation. A concurrent
identical request returns `IDEMPOTENCY_IN_PROGRESS`.

When an open carries a `route_binding`, that binding is part of the canonical
request fingerprint. LOC resolves it against the registry's authoritative
`SelectMany` result before minting and returns `route_binding_mismatch` if the
signed candidate no longer exists. LOC persists and replays the resulting full
route snapshot; an idempotent replay never re-runs route selection. Changing
the binding under the same key is request reuse, not failover.

LOC derives a stable payer `mint_request_id` from the durable request claim, so
an ordinary lost response can replay the daemon's recorded result. After the
in-flight timeout, LOC atomically reclaims the existing claim and retries only
that same mint ID; it never invents a replacement mint identity during
recovery. Concurrent recovery attempts therefore still have one winner.

The payer reserves a mint ID durably before signing and serializes calls sharing
that ID. A completed mint replays its exact response, allowing LOC to finish
the original business transaction once after a crash or lost daemon response.
If the payer crashed after reservation but before recording a response, it
returns `FAILED_PRECONDITION` instead of signing again. LOC records that as the
stable terminal `IDEMPOTENCY_OUTCOME_UNKNOWN` result and tells the customer to
start a new intent with a new `Idempotency-Key`. This is deliberately
fail-closed: an unavailable response costs a new intent; guessing on a possibly
signed ticket risks paying twice. LOC retains outcome-unknown claims as
permanent tombstones rather than deleting them with ordinary terminal replay
records, so the old customer key can never silently become a fresh mint.

A refill uses the same durable key across both mutable hops. LOC returns its
stable `request_id` with the payment envelope; the SDK sends that value as the
broker's required `Livepeer-Request-Id`. An LOC retry therefore replays one
payer mint, and a broker retry replays one credit and lease extension. A new
refill intent gets a new key; transport retries of that intent do not.

For paid jobs, `max_total_units` is only the caller's pre-execution funding
ceiling. LOC reserves `bill(max_total_units)` and does not treat the estimate
as evidence of delivered work. Terminal accounting comes exclusively from the
broker-signed settlement. LOC verifies its unit against the route snapshot and
rejects signed units or billed value above the persisted ceiling; otherwise it
bills the signed actual amount and releases the unused reservation. LOC never
parses workload media to determine usage.

### Jobs that never reach broker admission

Once LOC returns a signed payment envelope, it cannot revoke it. The broker or
another holder may submit a winning ticket at any point in its chain validity
window, and neither a caller assertion, a broker refusal, nor a payee non-use
attestation proves otherwise. Ticket validity is governance mutable and the
contract evaluates the current value at redemption, so a mint-time
`expires_after_round` is telemetry rather than permanent retirement proof.
Governance can extend or revive an issued envelope. LOC therefore never
automatically refunds, releases on expiry, or attempts re-encumbrance.

Before applying a conservative full charge, LOC polls the snapshotted broker's
`GET /v1/exchange/{request_id}` using the request ID LOC created. `IN_FLIGHT`
and `ACCOUNTING_PENDING` remain pollable. `NO_RECORD` is silence;
after that silence LOC directly requests an attributable record from
`POST /v1/non-admission/{request_id}` using scope from its immutable route and
payment records. LOC verifies the signature, delegated key, request/work/payment
identities, full quote reference, broker identity, observation time, and record
coverage before retaining `NOT_ADMITTED` as append-only audit evidence. Invalid
or unverifiable evidence remains unresolved. `ADMITTED_OUTCOME_UNKNOWN` and
`ADMITTED_EVIDENCE_EXPIRED` both prove admission without usable settlement
evidence. None authorizes a refund or an accounting mutation.

Only `SETTLED` carrying an original signed settlement can close the job
accurately. LOC ignores unsigned response hints and verifies the signed request
ID, broker job ID, work ID, work unit, unit totals, quote identity, billing
curve, signature, and snapshotted delegation before changing financial state.
A mismatched or `DEBIT_FAILED` claim leaves the job encumbered.

If no valid signed settlement is recoverable by the configured operational
deadline, LOC may finalize a distinct `conservative_full_charge`. That outcome
must never be represented as broker-settled usage, a successful network debit,
or fabricated work units. Signed non-admission remains attributable audit and
dispute evidence, not refund authority. The deadline is configured with
`JOB_CONSERVATIVE_CHARGE_AFTER_SECONDS`; its safe default is `0` (disabled), so
operators must select and document a nonzero billing policy deliberately.
An unreachable broker, timeout, or malformed lookup response is retained as a
LOC-observed `LOOKUP_FAILED` result: it is not broker evidence, but it also must
not bypass an operator's configured deadline forever. LOC retries it before the
deadline and applies the same distinct conservative outcome after the deadline.

There is deliberately no customer-authorized `abandon` endpoint. A broker
refusal may improve telemetry but cannot release money because a broker that
received the envelope could retain it and submit it later. Neither chain
telemetry nor broker assertions retire the exact envelope under the current
contract. Automatic refund requires immutable ticket validity or a
per-envelope retirement mechanism.

Refill funding follows the Modules cumulative ceiling curve. For cumulative
target `U`, `bill(U) = ceil(U × amount_wei / per_units)`; a refill requests
`bill(U_after) - bill(U_before)`. Rounding each increment independently is a
billing error because it can overfund by up to one wei per refill.

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

### Broker rejects a session top-up

If a broker returns `recipient_rotated` for a session top-up, the SDK asks LOC
for one fresh refill intent bound to the rejected `work_id` and request ID.
LOC reports the rejection to the payer daemon, which rotates the recipient;
the SDK then sends the successor payment with `Livepeer-Rebind-From`. A
successful rotation is settlement-only infrastructure and produces no
customer-visible event. A refused rebind drains with `payment_unrecoverable`.
LOC never retries unbound and never charges the refused payment twice.

The payer may also rotate proactively while minting a normal refill. Its
`CreatePaymentResponse` then carries the exact predecessor and successor work
IDs. LOC accepts this only when the predecessor equals the locked session's
current work ID, atomically advances the rotation generation, and returns the
same `rebind_from` contract to the SDK. A self-reference, stale predecessor, or
silent work-ID change fails closed. This path does not mark an earlier payment
refused because no payment was rejected.

### `payment-daemon.CreatePayment` returns an error

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
