# DESIGN.md

The load-bearing design decisions behind PymtHouse, written down so future
agent runs can reason about them without re-deriving them from code.

## What PymtHouse is

A single Python service that sits between Livepeer application developers and
the Livepeer payment infrastructure. It does four things:

1. Authenticates app developers (API keys) and operators (web sessions).
2. Tracks wei-denominated credit per user.
3. Discovers orchestrators and capabilities via `service-registry-daemon`.
4. Mints signed Livepeer payment tickets via `payment-daemon`, charging the
   user's credit by ticket expected value (EV) at issuance.

## What PymtHouse is not (MVP)

- Not a custody product for user-supplied USDC. The operator funds one pooled
  wallet; user balances are internal bookkeeping in wei.
- Not an OIDC identity provider. API keys only at MVP.
- Not a quoting engine. Routes returned by `service-registry-daemon.Select()`
  already carry the `quote_ref` PymtHouse passes through to `CreatePayment`.
- Not a redemption-watcher. Charge happens at ticket issuance; on-chain
  redemption is not observed by PymtHouse.
- Not horizontally scalable. Single instance; APScheduler in-process; single
  pooled wallet at the daemon.
- Not a marketing site. The `web/portal/` and `web/admin/` SPAs are utility
  surfaces for users and operators, not landing pages.

## Load-bearing decisions

### 1. Credit is denominated in wei

The unit of account in PymtHouse is the same unit Livepeer's payment system
uses on the wire. No FX conversion, no abstract "credits." When a future
version adds fiat (Stripe) or USDC rails, those are *deposit adapters* that
buy wei — they do not change the unit of account.

**Why:** simplest possible accounting. The number a user sees as "remaining
credit" is the same number the daemon sees as `expected_value` and the same
number that hits the user's spend-period ledger.

### 2. PymtHouse is custodial at the network boundary, not the user boundary

The operator funds one pooled signing wallet. That wallet is owned by
`payment-daemon` (V3 keystore loaded at boot — see `docs/SECURITY.md`).
Every ticket minted for every user is signed by that same wallet. Users hold
no on-chain key; they hold a credit balance in PymtHouse's database.

**Why:** removes wallet management from the app developer's surface
entirely. The user's "experience of payment" is depositing credit (in a
future version) or receiving credit from the operator (in MVP).
On-chain identity is the operator's, not the user's.

**Trade-off:** PymtHouse must absorb short-term variance from probabilistic
micropayments. See `docs/RELIABILITY.md`.

### 3. Charge ticket EV at issuance ("Option A")

When `payment-daemon.CreatePayment` returns `expected_value`, PymtHouse
decrements the user's balance by that exact amount and never revisits.
On-chain redemption outcomes are not observed.

**Why:** removes a whole feedback loop. No need for the orchestrator to
report back, no need to watch the chain for redemption events, no race
between "ticket issued" and "ticket settled." The user sees a deterministic
bill. The operator's wallet, integrated across all users, pays out roughly
what was charged in EV terms.

**Trade-off:** PymtHouse's pooled wallet eats short-term lottery variance —
favorable in expectation, unfavorable in any given window. Acceptable for
MVP scale; revisit if it stops being acceptable.

### 4. Job-sizing is "N work units," not "X wei of funding"

The app developer asks for tickets in units of work (e.g., "200 tokens",
"30 video frames"). PymtHouse multiplies by `price_per_work_unit_wei` from
the chosen route to compute funding, then calls `CreatePayment`. The wei
amount is a derived value, not a primary input.

**Why:** matches the app developer's mental model. They know how big their
job is; they don't know what wei rate the orchestrator is currently quoting.
Discovery (`Select`) gives PymtHouse the rate; PymtHouse does the math.

**Trade-off:** for request/response jobs where actual work isn't known
up-front (e.g., LLM completions), the app dev declares `max_work_units`,
PymtHouse reserves credit for the max, and refunds the delta after the app
dev reports actuals back. See `domains/usage`.

### 5. Discovery is a thin pass-through

`service-registry-daemon` is the source of truth for capabilities,
orchestrators, prices, and route fingerprints. PymtHouse adds auth and
passes the result through unmodified for MVP.

**Why:** any caching/filtering/availability logic on top is value PymtHouse
might want to add later, but it isn't load-bearing for a working ticket-mint
loop. Adding it now would tightly couple the registry view to billing
state. v2.

### 6. Fail closed, always

If the credit decrement would fail, if the spend-window cap would be
breached, if the daemon returns an error, if a usage write can't be made
idempotent — return an error (`402 Payment Required`, `5xx`, or
domain-specific). Do not serve work. See `docs/RELIABILITY.md`.

**Why:** under-billing a user is a liability; over-billing is recoverable
via an admin refund. The asymmetry favors fail-closed.

### 7. Layered architecture per domain, enforced mechanically

Every domain in `src/pymthouse/domains/` follows
`types → config → repo → service → runtime → ui`. Cross-cutting concerns
enter only through `src/pymthouse/providers/`. See `ARCHITECTURE.md`.

**Why:** the rule is one a coding agent can hold in its head and a linter
can enforce. It keeps the codebase navigable as it grows; it keeps the
"where do I add this" decision boring.

### 8. The frontend is intentionally minimal

Lit 3.2, vanilla CSS with `@layer`, light DOM only, no build step,
`esm.sh` for imports. No React, no Tailwind, no design system framework.

**Why:** lets an agent edit a file and see the change immediately. Removes
a whole toolchain (bundler, transpiler, css processor) from the surface
area. Matches the conventions established in `livepeer-modules-openai`.
See `docs/FRONTEND.md`.

### 9. PymtHouse fronts every external call from app devs

App developers talk only to PymtHouse. They never address `payment-daemon`
or `service-registry-daemon` directly. PymtHouse can therefore enforce
billing on every call without trusting clients.

**Why:** keeps the auth/billing model simple. A v2 might issue scoped
tokens for app devs to talk to daemons directly (taking PymtHouse out of
the hot path); that's an optimization, not a feature.

## When in doubt

Lean on the boring choice. Lean on the explicit boundary. Lean on the
fail-closed default. If you find yourself adding a layer because "we might
need it" — don't.
