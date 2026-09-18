# Tech Debt Tracker

A registry of known deferrals and identified debt. An item here is an
acknowledgement, not a promise to fix. When an item is addressed, link
the exec-plan that addressed it and remove it from this file.

## How to use

- **Add** an item when an exec-plan or design decision deliberately defers
  something. Note *what* was deferred, *why*, and *what would trigger
  doing it*.
- **Reference** items here from the exec-plan that introduced them.
- **Remove** items when they're resolved; the resolution lives in the
  exec-plan that resolved them, not here.

## Open items

### Discovery

- **No liveness checks on orchestrators returned by discovery.**
  `service-registry-daemon` returns routes without probing them; Livepeer Open Clearinghouse
  passes them through. *Trigger:* app-dev reports of dead-route payments.
- **Registry cache TTL is process-local.** `CachingRegistryClient`
  stores entries in process memory; multi-instance deployments do not
  share cache hits. *Trigger:* going multi-instance with a high read
  rate.
- **`Select(...) -> None` not cached.** A "no route" result re-hits the
  daemon every call (deliberate, so a freshly-published orch becomes
  visible without waiting out the TTL). May want to negative-cache with
  a shorter TTL if missing-route lookups become a hot path.

### Auth

- **No OIDC issuer.** API keys only. *Trigger:* a partner integration
  that genuinely needs OIDC for agent/desktop auth.
- **No per-key credit isolation.** All keys on a user share the user's
  credit pool. *Trigger:* a user wanting per-app billing separation.
- **OAuth flow has no portal "linked identities" UI.** A user can link
  Google and/or GitHub by signing in with each, but can't see or unlink
  them from the portal. *Trigger:* user requests for control over
  linked accounts.
- **No OIDC issuer (Livepeer Open Clearinghouse as identity provider for downstream
  apps).** We *consume* Google/GitHub but don't *issue* OIDC tokens.
  *Trigger:* partner integration that needs us as IdP.
- **Only owner/member roles; no viewer or per-resource scopes.** RBAC
  is two-tier: `owner` can manage operators, `member` can do everything
  else (approvals, topups, billing-config edits). No read-only role
  yet. *Trigger:* needing a support persona that can read audit/users
  without mutating anything.
- **No operator session UI, no SSO.** Operators sign in by pasting a
  bearer token into the admin SPA, which stashes it in localStorage.
  No password, no OIDC, no expiry, no idle timeout. *Trigger:* an
  operator pool large enough that bearer tokens become a security or
  ergonomics burden.
- **Operator email is plain `str` on output but `EmailStr`-validated on
  input.** The bootstrap row uses a `.local` TLD which pydantic's
  email-validator rejects, so output schemas had to relax. *Trigger:*
  if we tighten input validation further we should reconcile by
  switching bootstrap to a routable domain.
### Funding

- **No Stripe / USDC / on-ramp deposit flows.** Operator topup only.
  *Trigger:* operator needs to onboard users without manual topup; or
  a partner wants self-serve funding.

### Email

- **No "operator delivers initial API key by email" flow.** The
  original product sketch mentioned this; current build emails an
  approval notification and lets the user create their own key in the
  portal. *Trigger:* product decision to revisit operator-issued keys.
- **Resend 2xx-with-error detection covers one shape only.** The
  silent-failure guard in `providers/email/provider.py` looks for
  `{error: "..."}` (string) when the SDK returns 2xx. Other
  Resend-compatible backends may use `{errors: [...]}` (array) or
  `{status: "failed"}`. *Trigger:* second self-hosted backend with a
  different error envelope.

### Payments

- **Per-ticket redemption observation still absent.** Phase 18 polls
  the daemon's TicketBroker deposit/reserve and records snapshots, but
  Livepeer Open Clearinghouse does not see *individual* WinningTicketRedeemed events.
  Variance shows up as a delta between sum(payments.expected_value_wei)
  charged and the deposit drawdown observed in the snapshot series.
  *Trigger:* needing to attribute on-chain redemption to a specific
  payment row.
- **No ticket-redemption feedback to users.** Users don't see which of
  their tickets won. *Trigger:* user requests for visibility.
- **`Select` is called once per `CreatePayment`.** No batching, no
  pre-fetching of routes for the next mint. *Trigger:* latency budget.
- **Auto-replenish has no per-period maximum grant.** Both the
  proactive scheduler (`billing.service.run_auto_replenish`) and the
  reactive in-mint path (`payments.service._attempt_auto_replenish`)
  grant `auto_replenish_increment_wei` every time the threshold check
  passes. A misconfigured operator (low threshold + high increment +
  5-min cadence) could grant unbounded credit per period. *Trigger:*
  runaway-topup incident; or before exposing per-user config to
  end-users (so they can't self-configure into bankruptcy).
- **Auto-replenish ledger entries write `operator_id=None`.** The
  resulting `credit_ledger` rows are tagged `reason="auto_replenish"`
  but the operator audit-log UI (which queries `operator_audit`, not
  `credit_ledger`) can't surface them. *Trigger:* audit compliance
  reviewer asks "who funded this user?" and the trail dead-ends.
- **"Approve unverified" leaves no distinguishable audit trail.** The
  `operator_audit` row written by `admin.service.approve_user` records
  `action="approve_user"` whether the target was verified or not at
  approval time. *Trigger:* compliance review needs to know which
  approvals bypassed verification.

### Architecture

- **In-process APScheduler limits us to a single instance.** Auto-
  replenish and spend-window jobs run in-process. *Trigger:* needing more
  than one instance for HA or throughput.
- **No distributed locking on balance updates.** Postgres row-locking is
  sufficient because we run one instance. *Trigger:* multi-instance plan.
- **No schema-version / code-version startup check.** Nothing prevents
  the gateway from booting against a DB that's behind on migrations
  (or ahead, for a roll-back). *Trigger:* the first deploy where
  migrations don't run automatically.

### Observability

- **No Victoria-stack (LogQL / PromQL / TraceQL) integration.** Basic
  structured logs + `/metrics` only. *Trigger:* operator needs cross-
  service query capability for incident response.
- **No distributed tracing.** *Trigger:* same as above.
- **No alerting rules.** Operator runs raw Prometheus or eyeballs the
  admin dashboard. *Trigger:* first incident; alerting comes after.

### Admin

- **No operator-to-operator role separation.** All operators have full
  access. *Trigger:* org needs to split "approve users" from "set caps."
- **No webhook delivery for usage / payment events.** *Trigger:* a
  partner explicitly asks.

### Frontend

- **No design-system package.** CSS tokens are copy-pasted between portal
  and admin. *Trigger:* a third surface (mobile? embeddable?) appears.
- **No e2e test framework for the SPAs.** *Trigger:* a regression caught
  in QA that an e2e test would have caught.
- **Rust SDK still hand-types response shapes.** TypeScript, Python,
  and Go SDKs now generate their wire-shape types from the gateway's
  `/openapi.json` snapshot at the repo-root `openapi.json` (regen with
  `make refresh-openapi`). Rust was skipped: the standard generators
  (`openapi-generator-cli` is a Java tool; `progenitor` from Oxide
  emits a full client, not just types) are heavier than the
  hand-typed Rust SDK warrants. Rust's compile-time guarantees catch
  most drift bugs the other languages couldn't. *Trigger:* a Rust-
  side drift bug, or a third-party reqwest-friendly type-only
  generator becoming available.

### Security

- **Rate limiter is in-process only.** Buckets live in process memory;
  fine for single-instance MVP but does not enforce a global ceiling
  across replicas. *Trigger:* going multi-instance — swap the in-memory
  store for Redis without changing the call sites.
- **No 2FA on operator accounts.** *Trigger:* production deployment.
- **No keystore rotation runbook.** Documented inline in `docs/SECURITY.md`
  at a high level; no automation. *Trigger:* first time we need to rotate.

## Closed items

- **Bootstrap-operator-only admin auth** — replaced by full operator
  CRUD with `owner`/`member` role separation, hashed bearer tokens,
  rotation, revocation, and last-owner protection (2026-05-23).
- **Legacy `POST /v1/payments/mint` + `POST /v1/usage/report`** —
  removed in favor of handoff-mode `POST /v1/jobs` +
  `POST /v1/jobs/{id}/settle` per exec-plan 002. Endpoints deleted,
  `mint_payment` / `report_usage` service functions and their
  request/response types pruned, `domains/usage/runtime.py`
  removed, `domains/usage/service.py` + `types.py` removed
  (orphaned). `payment_idempotency_key` table + the
  `expire_stale_idempotency_keys` scheduler job kept as a
  no-op-effective drain for any pre-existing in-flight rows
  (2026-05-24).
- **SDK retry-on-INVALID_RECIPIENT_RAND** — replaced by the Modules v2
  recipient-rotation rebind handshake in all four SDKs. Silent-session
  recovery queries the broker by LOC's `gateway_session_id` and verifies the
  signed rotation/settlement chain; LOC no longer calls `GetSessionDebits`
  (2026-08-21).
