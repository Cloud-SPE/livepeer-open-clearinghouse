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

For long-running sessions, LOC's refill behavior depends on the
session's *mode* (declared by the offering's
`interaction_mode` in the registry):

- **`ws-realtime@v0`** — bounded. NO refill is possible. The
  initial mint funds the whole session; broker closes when balance
  hits zero. SDK fires `on_winddown_warning` when
  `Livepeer-Balance-Low` arrives, but cannot extend. Size
  `max_total_units` for the full session duration up front.
- **`session-control-plus-media@v0`** — extensible. SDK delivers
  the LOC-minted top-up via a `session.topup` JSON frame on the
  control WS.
- **`live-session-remote-runner@v0` / `live-session-gateway-ingest@v0`** —
  extensible. SDK delivers via `POST {control.topup_url}` to the
  broker (URL captured at session open).
- **`rtmp-ingress-hls-egress@v0`** — extensible. SDK delivers via
  the control WS (mirrors session-control-plus-media).

The SDK's `SessionRunner` (per language) handles the refill loop
automatically for extensible modes — see §5.

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

---

## 5. SDK criticality

Because LOC isn't in the data path, the SDK is **part of the
platform**. It's responsible for:

1. **Refill loop** (case d-extensible): subscribing to
   `Livepeer-Balance-Low` from the broker, calling LOC's refill
   endpoint, delivering the returned envelope back to the broker
   via the mode-specific channel.
2. **Settle reporting** (cases a/b/c): reading
   `Livepeer-Work-Units` from the broker response, posting to
   LOC's settle endpoint.
3. **Graceful close** on disconnect / shutdown / cap-refusal.
4. **Identity reporting** via the
   `Livepeer-Open-Clearinghouse-SDK: <lang>/<semver>/<git_sha7>`
   header on every LOC request.

The official SDKs (`sdks/python`, `sdks/typescript`,
`sdks/go`, `sdks/rust`) implement all of this. Custom
SDKs are tolerated for languages we don't ship but unsupported —
LOC's reconciliation janitor + daemon ledger compensate for
buggy/missing SDK behavior so the operator isn't left with
incorrect bills, but SLA / support tickets only honor official
SDK use.

The trust model (see design doc § "Trust model"): payer-daemon
`GetSessionDebits` is authoritative. SDK self-reports are
convenience for the synchronous path; the janitor cross-checks
out-of-band and corrects discrepancies.

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

**Symptom**: admin SPA's discrepancy-leaderboard view shows a
single API key (or a small cluster) with rising
`discrepancy_count` over the last hour.

**Triage steps**:

1. Pull the recent `server.discrepancy_detected` events for that
   API key:
   ```
   SELECT * FROM payment_settlement
   WHERE raw_record->>'reconciled_by' = 'janitor'
     AND created_at > now() - interval '1 hour'
     AND session_id IN (
       SELECT id FROM payment_session WHERE api_key_id = '<key>'
     );
   ```
2. Check the SDK identity for the API key (admin SPA → user
   detail → recent sessions). If it's a known-good version, the
   issue is likely upstream (broker debiting more than the SDK
   expected).
3. If SDK version is stale, contact the customer to upgrade.
4. If SDK version is current AND discrepancies are consistent,
   investigate the broker — call `GetSessionDebits` directly to
   confirm the daemon ledger is the right number, then check
   payee-side `payment-daemon` logs for the affected work_ids.

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
> mode-specific topup delivery, balance-low handling, graceful
> close).
>
> Custom clients are tolerated — the gateway's reconciliation
> janitor + daemon ledger ensure you'll never be billed
> incorrectly — but you forfeit SLA-grade support: incident
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
- Mode reference (upstream): the four "Case (d) modes" sub-table
  in the design doc § Q#1
- Upstream protocol repo:
  [`livepeer-cloud-spe/livepeer-network-modules`](https://github.com/Cloud-SPE/livepeer-network-modules)
- Per-SDK README: `sdks/{python,typescript,go,rust}/README.md`
- Per-language examples: `examples/{python,typescript,go,rust}/{one-shot-job,streaming-ws,streaming-http}/`
- Tech-debt + deferred items:
  [`docs/exec-plans/tech-debt-tracker.md`](exec-plans/tech-debt-tracker.md)
