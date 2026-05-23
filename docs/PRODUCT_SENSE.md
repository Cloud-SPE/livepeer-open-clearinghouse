# PRODUCT_SENSE.md

What Livepeer Open Clearinghouse is for, who it's for, and the scope guardrails.

## The pitch in one paragraph

If you are building an application that uses the Livepeer network — for
transcoding, AI inference, or any other capability orchestrators publish —
you should not have to manage an Ethereum wallet, fund a hot signer, mint
probabilistic micropayment tickets, or implement payment headers. You
should send your job, get your result, and get billed at the end. Livepeer Open Clearinghouse
is the service that makes that possible.

## Users

Livepeer Open Clearinghouse has two user populations.

### App developers

The primary user. Builds an application that consumes Livepeer network
capabilities. They:

- Sign up via web (email/password or Google/GitHub).
- Have their account approved by an operator.
- Receive an initial credit grant.
- Create one or more API keys, one per application.
- Call Livepeer Open Clearinghouse's HTTP API from their backend:
  - `GET /v1/capabilities` to discover what the network offers.
  - `GET /v1/orchestrators?capability=…` to find providers.
  - `POST /v1/payments/mint` to get a signed payment header for a job.
  - `POST /v1/usage/report` (optional) to reconcile actuals for variable-cost jobs.
- View their balance, recent payments, and per-key usage on the portal.

App developers never see an Ethereum wallet, a keystore, a private key, or
a ticket data structure. They see HTTP, JSON, and a balance in wei.

### Operators

The party running Livepeer Open Clearinghouse. They:

- Approve sign-ups.
- Set initial credit grants and per-user spend-per-period caps.
- Top up users manually (until automated funding rails ship).
- Fund the pooled signing wallet from outside Livepeer Open Clearinghouse.
- Monitor system health via the admin console and Prometheus metrics.

Operators are not the primary user. The admin console is a utility, not a
product.

## What we are building for MVP

The minimum that makes the value proposition real:

1. **Account onboarding.** Sign up, verify email, get approved, receive
   first API key.
2. **API key management.** Create, rotate, revoke. Per-key usage attribution.
3. **Credit accounting.** Wei-denominated balance per user. Operator topup.
   Auto-replenish bounded by a spend-per-period cap. Spend-window ledger.
4. **Discovery.** Pass-through of `service-registry-daemon.Resolve` /
   `Select` results, with the caller's API key validated.
5. **Ticket minting.** The headline path:
   `POST /v1/payments/mint { capability, offering, work_units }` →
   call `Select`, check balance, call `CreatePayment`, decrement EV, return
   `payment_bytes`.
6. **Usage reconciliation.** For variable-cost jobs, `POST /v1/usage/report`
   refunds the unused portion of a reserved payment.
7. **Operator admin.** Approve users, set caps, top up, see system status.
8. **Portal & admin SPAs.** Functional, not polished. Lit + zero-build.
9. **Single-container Docker Compose stack.** Postgres + two daemons +
   gateway. Pre-built images, no compose builds.

That's the bar. Anything beyond that is v2 unless it's load-bearing for one
of these items.

## What we are explicitly not building for MVP

- USDC allowance flows. No wallet connect, no `approve()`, no `transferFrom`.
- Stripe / MoonPay / Coinbase on-ramps.
- OIDC issuer for downstream services.
- Multi-issuer architecture.
- Team accounts / organizations.
- Per-key credit isolation (one credit pool per user; keys are attribution).
- Custom PreAuth smart contracts.
- A marketing site / landing page.
- A separately-hosted documentation site.
- Mobile clients / SDKs.
- Webhooks for usage events.
- Multi-tenant operator support (one operator, one stack).
- Horizontal scaling.
- LogQL / PromQL / TraceQL stacks (basic structured logs + `/metrics` is
  enough).

These all have legitimate reasons to exist eventually. None of them are the
critical path to "an app dev can integrate Livepeer Open Clearinghouse and pay an orchestrator
for work today."

## Scope guardrails

When evaluating new work, ask in order:

1. **Does it move "build the headline path" closer to done?** Ticket-mint
   from a real app-dev API call to a signed payment header is the headline.
2. **If not, does it make the headline path safer or more observable?**
   Fail-closed correctness, idempotency, telemetry — yes.
3. **If not, is it required by a guardrail (security, key custody, billing
   correctness)?** — yes.
4. **Otherwise — it's v2.** Add it to `docs/exec-plans/tech-debt-tracker.md`
   if it's worth remembering. Don't build it now.

## What success looks like

A new app developer signs up on a Monday, gets approved within an hour,
follows a one-page integration guide, and successfully mints a Livepeer
payment header from a `curl` example against their first real orchestrator
by Tuesday. They never read about Ethereum keys, ticket structures, or the
underlying daemon protocols.

That is the bar.
