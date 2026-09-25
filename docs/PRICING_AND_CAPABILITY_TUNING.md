# LOC pricing and capability tuning guide

Use this guide to decide how much work to admit, what prices to accept, how
much customer credit to reserve, and how much reusable credit to keep with
orchestrators. Start with a budget and measured workload; derive the settings
from those inputs.

This guide describes the wholesale-account implementation reviewed on
2026-09-24. Examples are sizing exercises, not market prices or deployment
defaults. Verify the deployed LOC, Modules, broker, and runner revisions
before applying settings. The governing contract is
[design 002](design-docs/002-fair-wholesale-credit-accounts.md); deployment
procedure lives in the [wholesale rollout runbook](references/wholesale-rollout.md).

## The five quantities to keep separate

| Quantity | What it controls | What it does not control |
|---|---|---|
| Accepted unit price | Cost of a unit of work at the selected offering | Number of jobs or streams |
| Job/session maximum | Cumulative authority for one engagement; customer credit held at admission | Reusable orchestrator account balance |
| Customer period cap | Admission exposure for one LOC user during a configured window | Exact on-chain payouts during that window |
| Wholesale float | Reusable credit in an independent settlement-domain account | Total spend over repeated replenishments |
| Ticket face value | Payout if one probabilistic ticket wins | Aggregate payouts from many winners |

Customer charging follows verified usage under the engagement's pricing
snapshot. Ticket expected value (EV) funds wholesale credit; it is not itself
a customer charge. Unused wholesale credit remains in its account after an
engagement ends. A customer's released hold is not a withdrawal of that credit.

## 1. Describe the capability before choosing numbers

For each offering, record its capability/offering identifiers, protocol,
work-unit name, quote numerator (`price_per_work_unit_wei`), denominator
(`units_per_price`), estimator, maximum workload, peak concurrency, and debit
cadence. Also record the supported media profile, session deadline, and which
payees/settlement domains may receive work. Use current discovery metadata and
the runner contract; capability names alone do not define billing semantics.

For units `U`, quote numerator `P`, and denominator `D`:

```text
wholesale_cost_wei = ceil(U × P / D)
normalized_price = P / D                 # wei per single work unit
max_units_for_budget = floor(budget_wei × D / P)  # when P > 0
```

Use integer or decimal arithmetic, preserving the denominator and rounding
up only where the contract bills. Do not use binary floating point for money.
For a zero-priced offering, bound units and concurrency explicitly even
though the price formula supplies no limit.

### ABR and live video are different meters

The companion transcode gateway/runner implementation reviewed for this guide
uses these contracts. Confirm them against your deployed discovery response:

| Offering | Work unit | Sizing rule |
|---|---|---|
| `video:transcode.abr` / `abr-default` | `video-frame-megapixel` | `ceil(sum(output_frames_i × width_i × height_i) / 1,000,000)` |
| `video:transcode.live` / `gateway-ingest` | `output_seconds` | Whole metered seconds from finalized output, under the advertised profile |
| Live AI or another capability | Offering-specific | Use its declared meter and estimator; do not inherit either video formula |

Estimate ABR frames from duration and each rendition's FPS. Sum the outputs;
input resolution alone is insufficient. For pixel-based billing, reducing
resolution, FPS, or rendition count reduces units. Lowering bitrate alone
does not proportionally reduce units. For live output-second pricing, a
different ladder may require a different offering/quote rather than a simple
pixel multiplier. Do not multiply live seconds by rendition count unless the
contract explicitly requires it.

For a pilot, 30 FPS and a 720p/480p/360p ladder are reasonable quality/cost
choices if supported by the offering and suitable for the source. Avoid
upscaling low-resolution sources. Choose higher quality from measured need.
Encoding quality, HLS segment duration, GOP, latency, and storage/CDN costs
remain runner/client concerns; LOC does not configure those media parameters.

## 2. Choose customer pricing and admission budgets

**Current configuration boundary:** new engagements snapshot
`wholesale_pass_through` pricing. The billing model and calculator also support
`cost_plus` and `unit_price`, but this checkout does not expose a general
operator pricing-plan selector. Do not invent environment variables or edit
stored snapshots to enable a markup.

| Pricing model | Customer charge for verified usage |
|---|---|
| Pass-through (current admission path) | Verified wholesale debit |
| Cost plus (model/calculator support) | `ceil(wholesale_debit × (10000 + fee_basis_points) / 10000)` |
| Unit price (model/calculator support) | `ceil(actual_units × retail_price / retail_units_per_price)` |

A future pricing-plan integration must hold the retail maximum and keep the
wholesale authorization independent. A 20% cost markup is 2,000 basis points;
it is not a 20% gross margin. Budget infrastructure, support, gas, and payment
variance separately when deciding whether a retail price covers costs.

Set the maximum work units to the smaller of the intended workload bound and
the units affordable under the per-engagement budget. Allow estimation
headroom only within that budget. LOC holds the maximum at admission, so a
huge maximum can reject a small job for insufficient credit. A cap increase
requires additional authority and an incremental hold; account replenishment
alone does not increase the engagement maximum.

| LOC setting | Operator choice |
|---|---|
| `DEFAULT_INITIAL_CREDIT_WEI` | Keep `0` for a manually funded pilot |
| `DEFAULT_SPEND_PERIOD_SECONDS` | `86400` for daily windows; this is not a rolling 24-hour limit |
| `DEFAULT_SPEND_PERIOD_CAP_WEI` | Explicit positive customer allowance; **`0` means unlimited** |
| `AUTO_REPLENISH_CHECK_INTERVAL_SECONDS` | `0` disables scheduled customer credit grants for a manual pilot |
| `AUTO_REPLENISH_INCREMENT_WEI` | Keep `0` unless intentionally granting recurring credit |

Inspect `GET /v1/admin/users/{user_id}/billing-config`: its `effective` values
resolve per-user overrides and defaults. The corresponding `PUT` replaces
the override fields; preserve desired fields when updating. A per-user zero
cap means unlimited, while a null override inherits the default. These caps
are per LOC user, not per API key, capability, or deployment.

**Period accounting is conservative:** the engagement maximum and subsequent
cap increases consume the window allowance when held. Releasing unused credit
restores the balance but does not restore that window's allowance. Repeatedly
opening oversized sessions can exhaust the cap despite low actual usage.
Authority admitted before a window boundary can continue afterward; this is
not an exact limiter on work performed within each calendar day.

Customer topups grant internal ledger credit. They do not fund the payer's
on-chain deposit or refill an orchestrator's account. If the business budget
starts in dollars, explicitly convert it to a wei allowance using an approved
exchange-rate assumption and review cadence; LOC does not maintain a USD cap.

## 3. Size reusable wholesale float

Let `R` be peak aggregate wholesale burn in wei/second for an account,
including all concurrent work. Let `L` cover polling delay, funding latency,
retry time, and scheduling jitter. Let `B` cover the largest additional debit
or reservation burst that must be admitted during that interval.

```text
low_water >= R × L + B
target > low_water
shortfall = target - observed_available  # only when available < low_water
```

For a smooth live pilot, start by evaluating a five-minute target and a
two-minute low-water mark. These are proposed operating margins, not guarantees.
ABR jobs can run faster than media playback and debit/reserve in large chunks;
size for their actual broker behavior, not average video duration. The reviewed
broker job middleware requests the full authorization maximum as its admission
reservation. Available account credit must cover that maximum, even if expected
actual usage is smaller. Ensure the float can admit the required reservation.
If it cannot, reduce job size or
concurrency, or deliberately raise exposure.

| LOC setting | Sizing rule |
|---|---|
| `WHOLESALE_CHAIN_ID` | Actual chain; `42161` for Arbitrum One |
| `WHOLESALE_TARGET_AVAILABLE_WEI` | Desired available balance per account |
| `WHOLESALE_REPLENISH_BELOW_WEI` | Low-water threshold derived above |
| `WHOLESALE_MAX_AVAILABLE_PER_PAYEE_WEI` | Allowance across that payee's settlement domains, including funding headroom |
| `WHOLESALE_MAX_AGGREGATE_AVAILABLE_WEI` | Explicit total exposure allowance across accounts/payees |
| `WHOLESALE_MAX_SINGLE_FUNDING_WEI` | At least the target if an empty account must bootstrap in one operation |
| `WHOLESALE_REPLENISH_CHECK_INTERVAL_SECONDS` | Start at `5`; measure that funding completes before runway expires |
| `SESSION_AUTHORIZATION_TTL_SECONDS` | Longer than the admitted session lifetime, including startup delay and margin |

These settings are global in this checkout, not independent per-capability
profiles. Mixed workloads must fit the shared policy. Target applies to the
selected account; per-payee and aggregate limits can prevent funding another
domain even when that account is empty. Extra failover destinations consume
float because old balances remain at their original domains.

LOC rejects an oversized shortfall rather than automatically splitting it
into smaller mints. Production requires positive wholesale settings, a
threshold no greater than target, and target no greater than either exposure
limit. Do not disable the authoritative wholesale polling loop merely because
SDK balance events also request replenishment.

The broker's realized credited value can exceed the requested shortfall due
to ticket granularity. Observe actual credit, pending/uncertain funding, and
exposure-limit breaches through `GET /v1/admin/wholesale`; projected limits are
not a guarantee of exact post-ticket balances. Keep headroom based on observed
ticket parameters rather than assuming all funding is exact.

## 4. Configure the payer daemon's independent limits

Verify flag support in the deployed binary. The reviewed Modules sender has:

| Flag | Scope |
|---|---|
| `--max-payment-wei` | Requested funded value and signed batch EV for one mint |
| `--max-authorization-wei` | Maximum cumulative wholesale debit for one authorization |
| `--max-ticket-face-value-wei` | Maximum face value of one winning ticket |
| `--max-price-per-unit` | Mint-time normalized price ceiling, keyed by exact work-unit name |

For the price flag, the syntax is
`video-frame-megapixel=<wei-per-unit>,output_seconds=<wei-per-unit>`.
Values are whole wei per single unit, not quote numerators per arbitrary
denominator. Units absent from the map have no rate ceiling. Two offerings
sharing a unit also share this daemon ceiling; keep stricter offering-specific
price acceptance in the calling application.

**Price enforcement limitation:** the reviewed sender checks price in
`CreatePayment`; `CreateSpendAuthorization` checks the cumulative debit cap
but does not apply the same unit-price policy. Reused wholesale credit can
therefore support authorization without a new mint-time price check. Apply
an explicit quote acceptance policy before requesting work and verify the
binding to the accepted route. Do not treat this flag as a universal
per-engagement price guard.

Set the authorization limit from the largest permitted wholesale workload.
Set the payment limit from the maximum funding shortfall plus acceptable
ticket EV granularity. A very small EV limit or face-value limit may reject
an otherwise affordable offering's ticket parameters. Measure the returned
batch EV and face values before increasing limits.

LOC's Compose file currently wires `MAX_PAYMENT_WEI` to `--max-payment-wei`.
The other flags require explicit command/deployment wiring; adding arbitrary
environment variables will not activate them. The sample `MAX_PAYMENT_WEI`
of 0.01 ETH is not an operator budget recommendation.

Preserve the sender database across restarts. Fund on-chain deposit/reserve
for the accepted ticket parameters and maintain transaction gas as needed.
Ticket EV is an expectation: several winners can produce cash outflow above
that expectation in a short window. Neither per-mint EV nor per-ticket face
value supplies a hard aggregate daily wallet-spend limit.

## 5. Worked sizing examples

All prices below are invented arithmetic examples. Replace them with accepted
quotes, and confirm ticket feasibility before deploying any calculated limit.

### One live stream, 15-minute maximum

Assume `P = 60,000,000,000,000 wei` per `D = 60 output_seconds`.
The normalized rate is `1,000,000,000,000 wei/second`.

| Decision | Derived value |
|---|---|
| Session maximum | `900` units |
| Wholesale authorization and current pass-through customer hold | `900,000,000,000,000 wei` (0.0009 ETH) |
| Estimated initial runway | `120` units; independent of lifetime authority |
| Five-minute account target | `300,000,000,000,000 wei` |
| Two-minute low-water mark | `120,000,000,000,000 wei` |
| Minimum single-funding allowance for empty-account bootstrap | `300,000,000,000,000 wei` |
| Illustrative customer window cap | `3,600,000,000,000,000 wei`: four full 15-minute admissions |

The daemon's authorization ceiling must admit 0.0009 ETH; its mint ceiling
can be smaller because replenishment funds only shortfall. Select per-payee,
aggregate, EV, and face-value headroom from ticket observations. A one-hour
authorization TTL (`3600`) gives this 15-minute pilot room for startup; verify
the actual session lifecycle. Do not use that TTL for longer sessions.

The companion video gateway uses `LIVE_MAX_TOTAL_UNITS` for the lifetime
ceiling. Its reviewed default `6000` means 100 minutes, not milliseconds.
`estimated_runway_units` is a LOC API field, not a LOC environment variable.
Gateway reconciliation and topup timing are separate settings in that
application. Increasing runway must be supported by funded account credit.

### One ten-minute ABR job

Assume three outputs at 30 FPS: 1280×720, 854×480, and 640×360.

```text
pixel_frames_per_second = 30 × (1280×720 + 854×480 + 640×360)
                        = 46,857,600
ten_minute_units = ceil(600 × 46,857,600 / 1,000,000) = 28,115
one_hour_units  = ceil(3600 × 46,857,600 / 1,000,000) = 168,688
```

With an invented quote of `1,000,000,000 wei` per megapixel-frame unit,
the ten-minute estimate costs `28,115,000,000,000 wei`. A proposed maximum
of `31,000` units admits modest estimator headroom and holds
`31,000,000,000,000 wei`. Confirm the runner's real estimator and frame counts;
the cap is allowed work, not a guarantee that every input completes.

The companion gateway exposes `ABR_MAX_TOTAL_UNITS` (reviewed default
`1000000`). It passes the configured ceiling as job authority, so tune it
to the largest intentionally admitted job. Its job timeout controls waiting,
not proof of non-execution. A timeout must trigger reconciliation under the
same durable request identity, not a new paid attempt with a new ID.

For simultaneous ABR and live traffic, include ABR reservation/debit bursts
in the account target and low-water calculation. The live-only table above
does not establish sufficient float for arbitrary ABR jobs.

## 6. Validate and adjust one constraint at a time

Start with one live stream and one ABR job as explicit client admission
limits, then measure them individually before running both together. LOC's
HTTP rate limits are not a concurrent-work or video-duration budget.

Run a short canary through admission, media ingest, playable output, signed
terminal settlement, and customer hold release. Confirm the exact quote,
units, customer maximum, wholesale shortfall, credited amount, and actual
charge. Exercise the intended maximum and observe a clean stop or refusal.
Verify a retry reuses its request identity and does not duplicate authority,
funding, or billing. Check recovery with the client disconnected so SDK
callbacks are not the only evidence of completion.

Measure peak burn and debit burst size, funding latency including retries,
minimum runway, failed replenishments, and unreconciled holds. After raising
concurrency, repeat the measurement because an account is shared. Preserve
the pricing assumptions and chosen values in your deployment's operator
configuration record, alongside image digests and the date of measurement.

| Symptom | Investigate before changing limits |
|---|---|
| Small workload refused for insufficient credit | Oversized maximum, existing holds, effective user cap, unused allowance not restored after release |
| Live stream stops while customer credit remains | Account runway, funding latency, authorization units/deadline, daemon refusal, media runner failure |
| Empty account cannot fund | Target exceeds single-funding allowance, projected exposure limit, deposit/reserve, ticket EV/face-value policy |
| Authorization succeeds but ABR admission returns 402 | Full job reservation exceeds broker available credit; inspect existing reservations, receipts, and settlement-domain alignment before attributing cause |
| Cheap quote but unexpectedly high cost | Wrong denominator, FPS/rendition count, estimator, repeated paid attempts, actual metered units |
| More payees cause funding refusals | Aggregate float already allocated to other accounts/domains |
| Daily customer allowance looks larger than usage | Maxima count when held; releases do not replenish period allowance |
| On-chain payouts exceed customer charges today | Ticket variance, previous wholesale funding, gas, or separate retail pricing; compare distinct ledgers |

For an overall operator daily budget, allocate user allowances, bound client
admission across capabilities, and monitor cumulative wholesale funding/debits
and wallet payouts separately. The current settings do not constitute a
single global daily on-chain spending breaker. Stop new admissions and cap
extensions when the operator budget is reached; already-issued authority and
existing tickets remain relevant exposure.

## Implementation references

- [LOC settings and production validation](../src/livepeer_open_clearinghouse/settings.py)
- [Customer holds, period accounting, and charge calculation](../src/livepeer_open_clearinghouse/domains/billing/service.py)
- [Pricing snapshot schema](../src/livepeer_open_clearinghouse/domains/billing/types.py)
- [Engagement pricing and session authorization](../src/livepeer_open_clearinghouse/domains/sessions/service.py)
- [Wholesale funding policy](../src/livepeer_open_clearinghouse/domains/wholesale/service.py)
- [Operator billing configuration endpoints](../src/livepeer_open_clearinghouse/domains/admin/runtime.py)
- [Environment example](../.env.example) and [Compose wiring](../docker-compose.yml)
- [Payment daemon contract and source locations](references/payment-daemon.md)

Companion source audited for the examples: `livepeer-modules-transcode-gateway`
(`.env.example`, `gateway/internal/server/paid_handlers.go`),
`livepeer-modules-transcode-runners` (`abr-runner/contract_v2.go`,
`live-runner/metering.go`), and `livepeer-network-modules/payment-daemon`
(`cmd/livepeer-payment-daemon/main.go`, `internal/service/sender/limits.go`,
`internal/service/sender/sender.go`). Those checkouts are not required to read
this guide; compare their deployed equivalents before applying companion flags.
