# RELIABILITY.md

The reliability principles for Livepeer Open Clearinghouse. Read this before changing
anything in `domains/billing`, `domains/payments`, or `domains/usage`.

> **Version boundary:** LOC admits new work only through the wholesale-account
> contract in
> [`002-fair-wholesale-credit-accounts.md`](design-docs/002-fair-wholesale-credit-accounts.md).
> Legacy rows may remain as closed audit history, but no legacy issuance,
> refill, or route fallback is supported.

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

Fail closed also when any selected
peer cannot prove support for the complete account, single-purpose
authorization, atomic reservation, and durable-status semantics. A partial
feature match is not a compatibility mode.

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
`IDEMPOTENCY_KEY_REUSE`, `IDEMPOTENCY_IN_PROGRESS`,
`IDEMPOTENCY_OUTCOME_UNKNOWN`, `AUTHORIZATION_REFUSED`,
`WHOLESALE_FUNDING_UNVERIFIED`, and `ENGAGEMENT_CLOSED`.

## Refused and abandoned authorizations

Creating a job or session commits the customer hold before the payer signs
the spend authorization, so a crashed request can be replayed with the same
`Idempotency-Key`. Recovery depends on what the payer proved:

| Payer result | LOC behavior |
|---|---|
| Definitive refusal (`FAILED_PRECONDITION`, `INVALID_ARGUMENT`, `PERMISSION_DENIED`, `OUT_OF_RANGE`) — nothing was signed | Close the engagement as `NOT_ADMITTED`, release the whole hold, return `422 AUTHORIZATION_REFUSED` |
| Transport failure or unknown outcome | Keep the claim and hold; a same-key retry resumes it |
| Request died before any authorization | The session reconciler releases the claim as `NOT_ADMITTED` once it is older than twice `IDEMPOTENCY_INFLIGHT_TIMEOUT_SECONDS` (minimum 10 minutes) |

A released claim is never authorized later: recording the first grant takes
the engagement row lock and rejects anything no longer `open` with
`409 ENGAGEMENT_CLOSED`, so a slow request and the reconciler cannot both
win. A cap revision refused on a running session returns
`AUTHORIZATION_REFUSED` without closing the session; its incremental hold
rolls back with the request.

Unproven broker account funding (`WholesaleFundingPolicyError`) returns a
retryable `503 WHOLESALE_FUNDING_UNVERIFIED`. The authorization and minted
funding stay durable, so a same-key retry replays the exact payment bytes
instead of recording a terminal failure.

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

For historical ticket accounting, once LOC returns a signed payment envelope,
it cannot revoke it. The broker or
another holder may submit a winning ticket at any point in its chain validity
window, and neither a caller assertion, a broker refusal, nor a payee non-use
attestation proves otherwise. Ticket validity is governance mutable and the
contract evaluates the current value at redemption, so a mint-time
`expires_after_round` is telemetry rather than permanent retirement proof.
Governance can extend or revive an issued envelope. LOC therefore never
releases credit merely on ticket expiry or attempts automatic re-encumbrance.
Wholesale customer holds are independent from those tickets and follow the
authorization recovery rules below.

Before applying a conservative full charge, LOC polls the snapshotted broker's
`GET /v1/exchange/{request_id}` using the request ID LOC created. `IN_FLIGHT`
and `ACCOUNTING_PENDING` remain pollable. `NO_RECORD` is silence and
`ADMISSION_REJECTED` is an unsigned admission-refusal diagnostic, not zero-usage
evidence. After the persisted authorization expires, either outcome triggers
`POST /v1/non-admission/{request_id}` using the original immutable grant scope.
LOC verifies the signature, delegated key, request ID, authorization ID (the
wholesale work ID), payer/payee, full quote reference, broker identity,
settlement domain, observation time, and record coverage. The grant must match
the job and must not already be known admitted or settled.

Verified `NOT_ADMITTED` evidence observed at or after authorization expiry can
close the wholesale job with zero usage/charge, retire its grant as
`expired_unused`, and release the customer hold exactly once. This relies on
the broker contract to irrevocably fence the exact unused authorization before
signing. Proof is retained in append-only audit and close records; it does not
refund wholesale funding. Earlier proof remains audit-only. Missing, invalid,
or temporarily unavailable proof leaves the hold intact and recovery retryable.
Expiry alone and HTTP 402 never authorize a release. `ADMITTED_OUTCOME_UNKNOWN` and
`ADMITTED_EVIDENCE_EXPIRED` both prove admission without usable settlement
evidence. None authorizes a refund or an accounting mutation.

For admitted work, `SETTLED` carrying an original signed settlement can close
the job accurately. LOC ignores unsigned response hints and verifies the signed request
ID, broker job ID, work ID, work unit, unit totals, quote identity, billing
curve, signature, and snapshotted delegation before changing financial state.
A mismatched or `DEBIT_FAILED` claim leaves the job encumbered.

If no valid signed settlement is recoverable by the configured operational
deadline, LOC may finalize a distinct `conservative_full_charge`. That outcome
must never be represented as broker-settled usage, a successful network debit,
or fabricated work units. Historical ticket non-admission remains audit-only;
expired wholesale authorization recovery follows the verified-proof rule above.
The [September 24 ABR incident review](references/abr-admission-rejection-recovery.md)
records the rejection contract, production funding findings, and rollout checks.
The deadline is configured with
`JOB_CONSERVATIVE_CHARGE_AFTER_SECONDS`; its safe default is `0` (disabled), so
operators must select and document a nonzero billing policy deliberately.
An unreachable broker, timeout, or malformed lookup response is retained as a
LOC-observed `LOOKUP_FAILED` result: it is not broker evidence, but it also must
not bypass an operator's configured deadline forever. LOC retries it before the
deadline and applies the same distinct conservative outcome after the deadline.

Two rules keep the reconciler honest about what it cannot fix. A route
whose snapshot carries no `settlement_keys` is refused at open
(`no_settlement_delegation`), because nothing minted against it could ever
verify. And when a recovered broker record fails verification against the
immutable snapshot, the reconciler records a `settlement_block` (reason, the
record's signature, first/last seen) on the row, emits one
`server.settlement_verification_failed` event, and stops re-verifying that
record; only a different record, or an operator, changes the outcome.

Long-running sessions use the same evidence rule. The attention view flags an
open session after two hours without a LOC-recorded successful funding event,
but elapsed time is only an operational alarm: it does not prove that the
broker lease ended and never releases a customer hold automatically. The view
also reports sessions closed in the last 24 hours after at least 60 seconds
with zero metered units, or any zero-unit session whose signed terminal
diagnosis is `output_failed`. The broker's signed settlement supplies the safe
termination reason, output state/timestamp, and failure code; LOC persists and
exposes those fields without relying on an SDK callback. Older brokers omit
the diagnosis and retain the duration-based compatibility behavior.

**Operator recourse.** `POST /v1/admin/jobs/{id}/resolve` closes an open job
or session on an explicit operator decision: `refund_hold` releases the
encumbrance, `accept_reported` charges the broker-reported units at the
snapshot price (the fair outcome when the broker did the work but the record
cannot be verified, e.g. a snapshot pinned before delegation), `charge_full`
charges the funded value. It writes the same settlement event and encumbrance
release a verified close would, plus an `operator_audit` row, and is
idempotent per row. The admin console exposes it on unresolved rows.

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

There is no customer usage-report endpoint. Usage is derived from settled
`payment_session` rows (see `docs/product-specs/006-usage-visibility.md`).

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
  on the user's `credit_balance` row inside the same transaction as the
  `credit_ledger` insert and engagement update. The locked query must refresh
  SQLAlchemy's identity-map value: admission may have loaded an unlocked
  preflight snapshot before waiting for a competing balance writer.
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

An extensible refill is a predecessor-bound authorization revision, not a new
ticket-session generation. The SDK preserves the LOC request ID and successor
authorization after any non-success response so the same operation can be
retried exactly. It never requests a recipient rotation or sends a
ticket-session rebind header. Bounded offerings refuse revisions and drain
their existing authorized runway.

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

### Pre-payment session preparation recovery

`POST /v1/sessions/prepare` selects a route and signs a preparation token; it
never holds customer credit, authorizes spending, or funds a broker. A known
`OpenClearinghouseError` during this step releases only that attempt for an
immediate identical retry. The claim's request fingerprint and broker request
ID remain durable, so changed content still returns `IDEMPOTENCY_KEY_REUSE`.

Release matches account, operation `sessions.prepare`, idempotency key, broker
request ID, `in_flight` status, the original lease expiry, and no payment ID.
It sets `expired` without changing the expiry. Preparation reclaim accepts that
explicit release immediately and advances the expiry beyond both the previous
lease and the new timeout. Keeping the old expiry as a lower bound prevents a
late failure from releasing a newer attempt even when retries share a clock
tick or the clock moves backwards. Reclaim also compares the observed expiry
atomically, so concurrent retries cannot both win.

This early-release rule does not apply to paid opens, refills, or uncertain
funding. Unexpected exceptions retain the existing timeout recovery behavior.
No rows or identities are deleted, and no migration is required.

Both registry selection RPCs have a configurable
`REGISTRY_SELECTION_TIMEOUT_SECONDS` deadline (default 45 seconds). `NOT_FOUND`
means no candidate; other gRPC statuses become sanitized `503
DAEMON_UNAVAILABLE` responses. Empty selections are not cached, so registry
recovery is visible on the next preparation attempt. A bound missing route
continues to return `409 route_binding_mismatch`, while an unbound missing route
returns `404 NO_ROUTE_AVAILABLE`. Keep caller and ingress/proxy HTTP timeouts
above the selection deadline with response-processing headroom, and keep the
claim timeout longer than selection. Increasing the registry budget alone
cannot extend a caller that still gives up after 30 seconds.

See [live preparation recovery](references/live-session-preparation-recovery.md)
for the incident review, RPC availability evidence, and coordinated deployment
procedure.

## Registry catalog discovery

LOC requires Modules `ListOfferings` (introduced at `e9f08e4`). Capabilities and
orchestrators, including admin views, use one shared catalog snapshot. No
`ListKnown`, `ResolveByAddress`, `Select` or `SelectMany` calls occur during catalog
refresh. An older daemon returning `UNIMPLEMENTED` yields 503; upgrade the daemon
before deploying this LOC version. Route selection for paid work remains separate.

`REGISTRY_DISCOVERY_RPC_TIMEOUT_SECONDS` bounds the snapshot RPC (default two
seconds). `REGISTRY_CATALOG_TIMEOUT_SECONDS` bounds each shared refresh (default
20 seconds, required below 30). Caller cancellation does not cancel work shared
with other requests. Cache invalidation prevents old refreshes repopulating it.
The cache TTL is capped by the snapshot's coverage and discovery validity bounds.

Both HTTP list responses retain `items` and add `catalog`: completeness, coverage,
snapshot/evaluation/source timestamps, discovery scope and `stale`. Coverage is
for the unfiltered daemon discovery scope. Lists show LOC-supported paid-job and
paid-session offerings selectable at the snapshot's `evaluated_at`; provider
identity, eligibility timestamps and constraints accompany each offering. Multiple
providers may advertise the same offering ID. Orchestrators are grouped by payee,
worker URL and worker ID, so separate brokers are not collapsed into one address.
Informational estimator metadata may omit executable fixture references; paid
route validation is unchanged.

Populated partial catalogs return 200 with completeness `PARTIAL`. Empty partial
or uninitialized results return `503 DAEMON_UNAVAILABLE`; an empty complete
catalog is a successful empty response. Filtering orchestrators does not change
the coverage scope or make partial negative evidence authoritative.

On refresh failure, a previous catalog may be returned for at most
`REGISTRY_CATALOG_STALE_SECONDS` beyond its original cache expiry (default 300
seconds). The fallback is labeled `stale=true`, `completeness=PARTIAL`, retains
original evidence timestamps, and emits `registry.catalog.stale_fallback`.
Fallback waits for the bounded refresh attempt; it does not renew snapshot age.
An empty fallback is inconclusive and returns 503. Set the stale allowance to zero
to disable fallback. A zero cache TTL disables stored snapshots but retains
refresh deadlines and concurrent request coalescing.

Catalog selectability and prices are informational observations, never payment
authority. Stale fallback does not apply to `Select` or `SelectMany`; their existing
route cache TTL and authoritative payment checks remain unchanged.

## Paid-job funding readiness

A paid-job broker reserves the authorization's entire maximum debit at admission.
Before returning that authorization, LOC plans account funding with an admission
floor equal to the maximum debit, even when available credit is above the routine
low-water threshold. The effective target is the greater of the normal float
and that reservation; the existing single-funding, per-payee and aggregate caps
remain binding. Existing durable claims, locked exposure checks and verified
receipt replay govern any mint. After completion, LOC rereads the exact receiver
account and requires sufficient available credit; otherwise it returns
`WHOLESALE_FUNDING_UNVERIFIED` without disclosing the grant.

Signing an internal grant is not funding proof. If a grant already exists when
readiness fails, retain its identity and hold for retry or authoritative
non-admission recovery. A balance observation cannot reserve credit atomically
against other clients; broker admission and signed recovery remain authoritative.
Do not silently reduce a caller's workload maximum to its estimate. See the
[ABR incident and cap guidance](references/abr-funding-readiness-2026-09-24.md).

### Live close after a refused revision

A signed terminal `authorization_exhausted` session record may report claimed
units above debited units. LOC accepts that gap only for wholesale authorization
accounting: actual and billed units must equal the debit, the charge must equal
`bill(debited_units)`, the remaining reservation must be zero, and all signature,
identity, quote, domain, ceiling, and sequence checks still apply. Claimed units
never determine the customer charge. Missing/zero settlement sequences remain
invalid for an initial close.

Before closing against a historical grant, LOC requires every newer grant to
be retired as `canceled_unused` or `expired_unused`. The authorization reconciler
reads the pinned broker's durable status, checks zero usage/debit/reservation,
and verifies its wholesale account domain, payer, payee, chain, and denomination.
Expiry evidence must be observed at or after the grant expiry. An already
admitted grant cannot become unused. `canceled_unused` relies on Modules'
persistent cancellation fence rejecting subsequent admission for that payer and
authorization. This is a TLS-bound receiver status contract, not a new signed
non-admission envelope. Account/status errors or inconclusive states preserve
holds and return retryable `503 successor_authorization_unresolved` from close.
The periodic authorization reconciler must run before close can succeed.

Close takes the engagement row lock shared with refill issuance. An exact retry
of the stored signed envelope, units, and compatible outcome returns the original
accounting result without a second ledger mutation or settlement event. Different
close evidence for an already-closed session returns 409. Older cached verifier
failures are reevaluated under the new exhaustion policy; transient successor
status failures are never cached as permanently invalid settlement evidence.

### Live admission funding readiness

Live open uses the immutable authorization's maximum debit as the receiver
available-credit floor, independently of the routine low-water threshold. LOC
raises the request's funding target to cover that floor without increasing
single-funding, per-payee, or aggregate exposure limits. Customer workload caps
are never silently reduced.

For a revision, the receiver atomically inherits the admitted predecessor's
billed amount and replaces its reservation. The additional available-credit
requirement is therefore `new_max_debit - predecessor_billed - predecessor_reserved`.
LOC queries that exact predecessor on the pinned broker, verifies its identity,
state and accounting bounds, and checks its persisted route/domain scope. It
rechecks predecessor state and receiver available credit after funding. Missing,
non-admitted, contradictory or unavailable predecessor evidence cannot justify
reusing any reservation.

Funding-policy failures, unavailable receiver observations and insufficient
post-funding credit return retryable `503 WHOLESALE_FUNDING_UNVERIFIED`. The
original grant, customer hold and mint identity remain durable. A retry recovers
the existing funding receipt rather than issuing a replacement payment. Routine
background replenishment continues using its configured target and threshold.
These readiness checks do not atomically reserve credit: another request can
consume it before broker admission; atomic admission remains tracked in `loc-8v4`.
