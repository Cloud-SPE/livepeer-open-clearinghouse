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
  `service-registry-daemon` returns routes without probing them; PymtHouse
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
- **No OIDC issuer (PymtHouse as identity provider for downstream
  apps).** We *consume* Google/GitHub but don't *issue* OIDC tokens.
  *Trigger:* partner integration that needs us as IdP.
- **Bootstrap-operator-only admin auth.** No operator UI, no operator
  CRUD, no operator-to-operator role separation. A single env var
  (`ADMIN_BOOTSTRAP_TOKEN`) gates the admin API. *Trigger:* needing
  more than one operator or wanting role separation.
### Funding

- **No Stripe / USDC / on-ramp deposit flows.** Operator topup only.
  *Trigger:* operator needs to onboard users without manual topup; or
  a partner wants self-serve funding.

### Email

- **No Resend webhook handling.** Bounces, complaints, and delivery
  events from Resend are not consumed. PymtHouse considers an email
  delivered if the synchronous SDK call succeeds. *Trigger:* email
  deliverability becomes a real concern (legitimate user reports
  "never got the verification email").
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
  PymtHouse does not see *individual* WinningTicketRedeemed events.
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
- **SDK types are hand-written, not generated.** The four reference
  SDKs declare response shapes by hand; mistakes go undetected because
  every SDK test stubs the HTTP layer with arbitrary JSON. This bit
  us once already — admin Catalog showed `Orchs=0` for every row
  because the JS handler treated `o.capabilities` as a list of
  strings when the gateway returns a list of objects. *Trigger:*
  fixing once via OpenAPI-driven codegen would eliminate the failure
  mode entirely (FastAPI exposes `/openapi.json` already).

### Security

- **Rate limiter is in-process only.** Buckets live in process memory;
  fine for single-instance MVP but does not enforce a global ceiling
  across replicas. *Trigger:* going multi-instance — swap the in-memory
  store for Redis without changing the call sites.
- **No 2FA on operator accounts.** *Trigger:* production deployment.
- **No keystore rotation runbook.** Documented inline in `docs/SECURITY.md`
  at a high level; no automation. *Trigger:* first time we need to rotate.

## Closed items

(empty)
