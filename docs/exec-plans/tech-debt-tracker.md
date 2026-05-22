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

- **No local caching of `service-registry.Select` results.** Every
  ticket-mint call makes a fresh `Select` RPC. *Trigger to address:*
  observable p99 latency on `/v1/payments/mint` regressing because of
  daemon latency.
- **No liveness checks on orchestrators returned by discovery.**
  `service-registry-daemon` returns routes without probing them; PymtHouse
  passes them through. *Trigger:* app-dev reports of dead-route payments.
- **`MockRegistryClient` is the only implementation as of Phase 4.** Real
  gRPC client against `service-registry-daemon` lands in Phase 6/7 with
  the docker compose stack. *Trigger:* moving past Phase 5.

### Auth

- **No OIDC issuer.** API keys only. *Trigger:* a partner integration
  that genuinely needs OIDC for agent/desktop auth.
- **No per-key credit isolation.** All keys on a user share the user's
  credit pool. *Trigger:* a user wanting per-app billing separation.
- **No OAuth (Google/GitHub) sign-in in Phase 4.** Email/password only.
  *Trigger:* user feedback that signup friction is too high.
- **No password reset flow.** Users who forget their password are stuck.
  *Trigger:* first support ticket.
- **Bootstrap-operator-only admin auth.** No operator UI, no operator
  CRUD, no operator-to-operator role separation. A single env var
  (`ADMIN_BOOTSTRAP_TOKEN`) gates the admin API. *Trigger:* needing
  more than one operator or wanting role separation.
- **No rate-limiting on signup/login.** Captures only on API key
  validation (per `docs/SECURITY.md`). *Trigger:* first abuse incident.

### Funding

- **No Stripe / USDC / on-ramp deposit flows.** Operator topup only.
  *Trigger:* operator needs to onboard users without manual topup; or
  a partner wants self-serve funding.

### Payments

- **No on-chain redemption observation.** PymtHouse charges EV at
  issuance and never reconciles against actual on-chain payouts.
  *Trigger:* the variance between charged EV and actual wallet payouts
  becomes operationally material.
- **No ticket-redemption feedback to users.** Users don't see which of
  their tickets won. *Trigger:* user requests for visibility.
- **`Select` is called once per `CreatePayment`.** No batching, no
  pre-fetching of routes for the next mint. *Trigger:* latency budget.
- **`GrpcPaymentDaemonClient` is a stub.** Phase 7 ships on
  `MockPaymentDaemonClient`. The path to real: run `make protoc` to
  generate stubs under `src/pymthouse/providers/payment_daemon/_gen/`,
  then implement the real gRPC dial + message mapping. Swap in
  `dependencies.py:_default_payment_daemon()`. *Trigger:* needing to
  actually pay a real orchestrator.
- **Per-user spend-cap overrides.** Currently the spend-window cap
  comes from the global `DEFAULT_SPEND_PERIOD_CAP_WEI`. Adding per-user
  overrides means a `user_billing_config` table and admin UI for
  editing. *Trigger:* an operator needs different caps for different
  users.
- **Auto-replenish is reactive only.** Triggers on a failed mint, not
  on a schedule. If a user mints constantly we always refill, but if
  they have a long-running balance trickle there's no proactive
  top-up. *Trigger:* operator wants users' balances kept above a
  threshold without inline retries.

### Architecture

- **Layered-architecture lint is not implemented.** Layering is enforced
  by convention only. *Trigger:* a real violation in a PR.
- **In-process APScheduler limits us to a single instance.** Auto-
  replenish and spend-window jobs run in-process. *Trigger:* needing more
  than one instance for HA or throughput.
- **No distributed locking on balance updates.** Postgres row-locking is
  sufficient because we run one instance. *Trigger:* multi-instance plan.

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

### Security

- **No rate-limiting on signup or password reset.** API key validation has
  rate-limiting per `docs/SECURITY.md`; user-flow endpoints don't yet.
  *Trigger:* first abuse incident.
- **No 2FA on operator accounts.** *Trigger:* production deployment.
- **No keystore rotation runbook.** Documented inline in `docs/SECURITY.md`
  at a high level; no automation. *Trigger:* first time we need to rotate.

## Closed items

(empty)
