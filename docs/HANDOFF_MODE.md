# Handoff-Mode Reference

The customer-facing + operator-facing reference for how jobs and
sessions work in Livepeer Open Clearinghouse since the
exec-plan-002 rewrite. Companion to:

- The design doc:
  [`docs/exec-plans/completed/002-long-running-sessions.md`](exec-plans/completed/002-long-running-sessions.md)
- The architecture overview: [`ARCHITECTURE.md`](../ARCHITECTURE.md)
- The reliability + state machines: [`docs/RELIABILITY.md`](RELIABILITY.md)

---

## 1. What handoff mode is

LOC is the **control plane**: it authorizes the customer, mints
payment envelopes, encumbers worst-case spend against the user
balance, accepts settle reports, and runs reconciliation.

LOC is **not** in the data plane: the customer's SDK talks to the
orchestrator-side broker directly using the minted envelope as the
`Livepeer-Payment` header.

This is the central operational fact. It changes the blast radius
of an LOC outage from "every active session dies" to "no new
mints; existing work continues." It also means **the SDK is
load-bearing for refills, settlement reporting, and graceful
close** — see §5.

---

## 2. The endpoint surface

| Path | Use |
|---|---|
| `POST /v1/jobs` | Open a one-shot job (cases a/b/c — atomic, post-settled, streaming) |
| `POST /v1/jobs/{id}/settle` | Report actual_units; reconcile billing |
| `POST /v1/sessions` | Open a long-running session (case d) |
| `POST /v1/sessions/{id}/refill` | Mint a top-up bound to an existing session |
| `POST /v1/sessions/{id}/close` | Finalize a session; refund unused |
| `GET  /v1/sessions/{id}` | Read-only customer status snapshot |
| `GET  /v1/payments/me` | Customer's payment history |
| `GET  /v1/payments/{work_id}` | Lookup by upstream work_id |

The customer SDK (`OpenClearinghouseClient.submit_job`,
`open_session`, etc.) wraps these. Direct HTTP is supported for
non-Python languages without an official SDK; see the OpenAPI doc
at `/openapi.json`.

---

## 3. Per-session caps (read this before tuning anything)

Per exec-plan 002 Q#3, LOC enforces a four-layer cap structure.
**Mint and refill checks evaluate against `max_total_units × EV`,
not against the initial runway.** All caps are enforced at v1.

| Cap | Always on? | Where it lives | What it bounds |
|---|---|---|---|
| **(i) User balance** | Yes | `credit_balance.balance_wei` | User can't run a mint that would drive balance negative |
| **(ii) Spend-period cap** | Yes | `billing_config.spend_period_cap_wei` | Rolling-window spend per user. Refills count against this cap too |
| **(iii) Per-session cap** | Yes | `payment_session.max_total_units` (the customer sets it at open) | Worst-case exposure of one session |
| **(iv) Operator-pool cap** | Opt-in | new operator-scope config, default disabled | Aggregate-spend circuit breaker across all users in a window |

At session open, LOC encumbers the **full worst case**
(`max_total_units × EV-per-unit`) from the user balance. This
guarantees per-session refill is bounded by construction: a refill
mid-session will only ever be refused because the spend-period cap
or operator-pool cap shifted — never because per-session
calculations changed. The encumbered value is released at close as
`funded − billed`.

### Tuning guidance for operators

- `spend_period_cap_wei = 0` (default) means **no cap**. Set this
  per user in `BillingConfig` to limit rolling spend windows.
- `auto_replenish_threshold_wei` + `auto_replenish_increment_wei`
  control automatic top-up from the operator pool when a user's
  balance dips. Off by default. The reactive in-mint path was
  removed during exec-plan 002 cleanup; auto-replenish now runs
  only via the scheduler (`billing.service.run_auto_replenish`).
- Operator-pool cap: not yet wired (v1.1 work); flag in
  tech-debt-tracker.

---

## 4. Refill policy (case d)

For long-running sessions, LOC requires `paid-session/v1` and persists the
offering's declared session axes at open. Refill behavior comes only from
`session.refill`:

- **`bounded`** — LOC never mints a refill. Size `max_total_units` for the
  complete session and drain when the broker advertises exhaustion.
- **`extensible`** — the SDK requests another envelope from LOC and submits it
  to the broker's authoritative HTTP `topup_url`. A control WebSocket may
  mirror state, but is not a separate delivery contract.

The SDK's `SessionRunner` (per language) handles the refill loop
automatically for extensible offerings — see §5.

The broker's open operation is idempotent. After an SDK process restart, the
runner repeats the same `POST /v1/session` with the original
`Livepeer-Request-Id`; the recorded response supplies a usable credential and
the current status, top-up, and end URLs. LOC does not persist broker
credentials or enter the broker control path.

### Refill refusal

When LOC refuses a refill (cap_reached or daemon failure):

- HTTP 402 with `error/which/remaining_wei/advice` in the response
  body
- The session is NOT immediately killed — broker will continue to
  drain whatever runway is left, then close on its grace window
  (default 10s = 2 ticks × 5s)
- SDK fires `on_refill_refused` callback if registered, then lets
  the session drain naturally
- Session exits with `outcome: "cap_reached"` once the broker
  closes

### Recipient rotation

`recipient_rotated` is a single bounded recovery handshake, not a generic
retry loop:

1. The SDK preserves the rejected LOC `work_id` and request ID.
2. LOC reports that exact rejection to the payer daemon and mints a successor
   under a fresh LOC and payer request identity. The refused payment is marked
   and cannot contribute a second charge.
3. The SDK sends the successor to the same broker session with
   `Livepeer-Rebind-From: <rejected-work-id>`.
4. The broker's signed terminal settlement carries the predecessor,
   generation, successor `work_id`, and cumulative session charge. LOC verifies
   the whole chain before final accounting.

A successful rotation is invisible to the customer; the settlement chain is
the audit record. If the broker refuses the declared rebind, the SDK emits one
`payment_unrecoverable` winddown warning and lets the funded session drain. It
does not attempt another rotation.

The payer can avoid the rejected-ticket round trip by rotating proactively at
its nonce boundary. In that case `CreatePaymentResponse.predecessor_work_id`
is non-empty only when the work ID actually changed. LOC requires it to equal
the session's locked current work ID, advances the generation exactly once,
and returns the ordinary rebind response to the SDK. The broker-facing steps
3–4 above are unchanged; there is no refused payment to refund in this path.

---

## 5. SDK criticality

Because LOC isn't in the data path, the SDK is **part of the
platform**. It's responsible for:

1. **Refill loop** (an offering with `session.refill=extensible`): consuming
   the normative `balance` object from broker status/top-up responses or the
   optional events WebSocket, calling LOC's refill endpoint, and delivering
   the returned envelope through the authoritative HTTP top-up URL. The SDK
   reuses the same request ID across both hops until delivery succeeds.
2. **Settle reporting** (cases a/b/c): reading
   `Livepeer-Work-Units` from the broker response, posting to
   LOC's settle endpoint.
3. **Graceful close** on shutdown or cap-refusal. An optional events WebSocket
   disconnect is not, by itself, authoritative session termination.
4. **Identity reporting** via the
   `Livepeer-Open-Clearinghouse-SDK: <lang>/<semver>/<git_sha7>`
   header on every LOC request.

The official SDKs (`sdks/python`, `sdks/typescript`,
`sdks/go`, `sdks/rust`) implement all of this. Custom
SDKs are tolerated for languages we don't ship but unsupported.
Every SDK must forward the broker-signed terminal settlement; LOC
fails closed if the envelope is missing, invalid, replayed, forked,
or inconsistent with its pinned route and session state.

The trust model (see design doc § "Trust model"): the broker-signed
`paid-session/v1` settlement chain is authoritative. SDK fields are
only consistency assertions. LOC does not use payer-daemon debit
polling for final accounting.

---

## 6. SDK approval-list rotation (operator runbook)

The `Livepeer-Open-Clearinghouse-SDK` header lets operators
distinguish customer integrations by language + version + git SHA.
The `sdk_approval` table stores the operator-curated allow /
deprecate / block list keyed on the `(lang, version, git_sha7)`
triple. Operator actions:

1. **Add or update a row** via:
   - `POST /v1/admin/sdk-approvals` — body `{lang, version,
     git_sha7, status, notes?}`. `status` is one of `approved`,
     `deprecated`, `blocked`.
   - `PATCH /v1/admin/sdk-approvals/{id}` — change status or
     notes.
   - `DELETE /v1/admin/sdk-approvals/{id}` — remove.
   Every mutation lands a row in `operator_audit`.
2. **List current approvals** via `GET /v1/admin/sdk-approvals`.
3. **Inspect recent sessions** via
   `GET /v1/admin/sessions/recent?limit=100` — each row carries
   the observed `sdk_identity` and the bucketed
   `sdk_status` (approved / deprecated / blocked / unknown).
4. **Aggregate by SDK identity** via
   `GET /v1/admin/sdk-distribution?limit=50` for dashboard panels
   showing population spread.
5. **Publish the approved list** via `GET /v1/sdk/manifest`
   (public, no auth — SDKs hit it at startup). Returns only
   approved + deprecated rows; blocked rows stay operator-internal.

Mismatches don't (yet) block mints — LOC can't enforce SDK
integrity remotely — but the data surfaces in admin so an
operator can reach out to the customer running an old or
non-conformant build. Mint-time enforcement on the `blocked`
status is a follow-up gated behind exec-plan 002's enforcement
phase.

The full approval-list spec, signing protocol, and operator UI
are documented in the design doc § "SDK criticality and
conformance".

---

## 7. Operator incident playbook: SDK discrepancy spike

**Symptom**: session close returns
`settlement_verification_failed`.

**Triage steps**:

1. Inspect the failure reason and the stored route snapshot's
   delegated settlement keys.
2. Compare the signed `gateway_session_id`, `session_id`, current and
   predecessor `work_id`, rotation generation, price, unit, and
   `settlement_seq` with the durable LOC session.
3. If the SDK omitted or malformed `Livepeer-Settlement`, require an
   SDK upgrade. If the signature or signed fields disagree, preserve
   the envelope and investigate the broker; do not finalize manually.

If the SDK disappears before close, LOC's slow reconciliation job queries
`GET /v1/settlement/{gateway_session_id}` at the pinned broker URL. It never
queries by `work_id`, because multiple logical sessions may share that payment
identity. A terminal envelope passes the same signature, identity, rotation,
price, unit, cap, and sequence checks as an SDK-forwarded close before LOC
releases any encumbrance. Missing, active, malformed, or mismatched records
leave the session open and fail closed financially.

**Self-protection**: per design Q#3, the LOC-side encumbrance is
worst-case at session open. Customers can never be billed more
than `max_total_units × EV-per-unit`, regardless of broker
behavior. The discrepancy log is for operator visibility, not for
customer billing protection.

---

## 8. Customer onboarding: official SDKs only

The customer-facing onboarding doc should make clear:

> Livepeer Open Clearinghouse expects you to use one of the four
> official SDKs (Python, TypeScript, Go, Rust). The handoff
> protocol — minting envelopes, calling brokers, reporting
> settlements, refilling sessions — depends on SDK behavior that
> custom clients are likely to get wrong (HTTP trailer reading,
> protocol top-up delivery, balance handling, graceful
> close).
>
> Custom clients are tolerated, but every close must forward the
> broker-signed terminal settlement and invalid evidence fails closed.
> You also forfeit SLA-grade support: incident
> response, latency guarantees, and the published broker quality
> scores all assume an official SDK is in use.
>
> If you need a language we don't ship, the OpenAPI document at
> `/openapi.json` is the authoritative wire contract. Use it +
> the SDK source as your reference implementation.

This text is suitable for inclusion in customer-facing onboarding
emails, the portal first-login flow, and the API docs landing
page.

---

## 9. Migration guide: legacy → handoff

If you're integrating against an old version of LOC that exposes
`POST /v1/payments/mint` + `POST /v1/usage/report`, here's the
1:1 mapping to the new handoff endpoints:

| Old | New | Notes |
|---|---|---|
| `POST /v1/payments/mint` | `POST /v1/jobs` | New endpoint mints + tells you the broker URL in one call |
| (call broker yourself with `Livepeer-Payment` header) | (call broker yourself with `Livepeer-Payment` header) | Identical pattern — the SDK still drives the broker call |
| `POST /v1/usage/report` | `POST /v1/jobs/{job_id}/settle` | New endpoint takes `actual_units` (was `actual_work_units`) and returns `cap_status` |
| (no equivalent — legacy didn't support refills) | `POST /v1/sessions` + `/refill` + `/close` | Long-running sessions are a new first-class concept |

Concrete code migration (Python):

```python
# OLD (deprecated):
mint = await client.mint_payment(
    capability="openai:chat-completions",
    offering="gpt-oss-20b",
    work_units=200,
)
# ... call broker ...
await client.report_usage(
    payment_id=mint.payment_id,
    actual_work_units=actual,
)

# NEW (handoff):
result = await client.submit_job(
    capability="openai:chat-completions",
    offering="gpt-oss-20b",
    estimated_units=200,
    max_total_units=2000,  # NEW — encumbrance ceiling
    body={"messages": [...]},
)
# `result` carries the broker response body, status, and the
# settlement — no separate report_usage call.
```

The new `submit_job` composes mint + broker-call + settle into a
single async function. Same for `open_session` for long-running
case-(d) workloads.

---

## 10. Pointers + further reading

- Design doc + decision log:
  [`docs/exec-plans/completed/002-long-running-sessions.md`](exec-plans/completed/002-long-running-sessions.md)
- Protocol references: upstream `paid-job/v1` and `paid-session/v1`
- Upstream protocol repo:
  [`livepeer-cloud-spe/livepeer-network-modules`](https://github.com/Cloud-SPE/livepeer-network-modules)
- Per-SDK README: `sdks/{python,typescript,go,rust}/README.md`
- Per-language examples: `examples/{python,typescript,go,rust}/{one-shot-job,streaming-ws,streaming-http}/`
- Tech-debt + deferred items:
  [`docs/exec-plans/tech-debt-tracker.md`](exec-plans/tech-debt-tracker.md)
