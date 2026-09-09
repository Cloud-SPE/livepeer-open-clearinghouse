# 002. Fair wholesale credit accounts

| | |
|---|---|
| Status | accepted — implementation pending |
| Opened | 2026-09-08 |
| Beads | `loc-1zq` |
| Governing Modules work | `lnm-b41`; `docs/design-docs/wholesale-credit-accounts.md`; plan `0049-fair-wholesale-credit-accounts.md` |
| Integration baseline | Modules commit `913cf7de10e5c090fd60ccc36234943210670f0d` (`wholesale-account` `1.0.0-draft`) |

## Decision

LOC will adopt the fair wholesale funding contract implemented by Livepeer
Network Modules. The user-designated integration baseline is Modules commit
`913cf7de10e5c090fd60ccc36234943210670f0d`; its protocol document identifies
the contract as `wholesale-account` `1.0.0-draft`. LOC must persist and
negotiate the wire version rather than infer support from daemon image tags.

LOC remains the Livepeer payer and each selected orchestrator remains the
wholesale payee. All LOC customers share LOC's wholesale credit account with a
given payee. Customer balances, holds, API-key attribution, spend caps, and
pricing remain isolated inside LOC and never become authority over that shared
account.

Tickets will add only a bounded shortfall to the shared wholesale account.
They will not encode how much an individual customer job or session may spend.
A separate, signed, single-purpose authorization will bound one engagement.
The broker will reserve against the shared account, debit actual wholesale
usage, and release unused reservation.

This document fixes LOC's responsibilities and migration constraints. It does
not redefine the authorization wire schema, canonical encoding, signatures,
endpoint names, status vocabulary, or negotiation mechanism. Those are owned
by the pinned Modules protocol and vendored protobufs.

## Pinned protocol boundary

LOC vendors the authoritative `types.proto` and `payer_daemon.proto` from the
baseline above and generates Python bindings with `make protoc`. The mapped
contract is:

| Modules contract | LOC boundary |
|---|---|
| `SpendAuthorizationPayload` / `SpendAuthorization` | Typed authorization request/response values in the payment-daemon provider; opaque signed bytes at customer and broker boundaries |
| `CreateSpendAuthorization` | Trusted LOC-to-payer-daemon signing call; customers never supply payer, payee, price, route, or signature fields directly |
| `AccountFundingIntent` on `CreatePayment` | Trusted target/observed available values used only after a locked broker account observation |
| `account_shortfall_wei` on `CreatePaymentResponse` | Fail-closed verification and wholesale funding evidence, distinct from customer authorization and charge |
| `WholesaleAccountView` | Parsed broker observation persisted with route, version, chain, denomination, and observation time |
| Authorization state and settlement extension fields | Durable reconciliation evidence keyed by LOC authorization plus request/session identity |
| `Livepeer-Authorization` | Base64 signed authorization sent to the locked broker |
| Optional `Livepeer-Payment` | Shortfall funding only on an account-authorized invocation |
| `extra.features.wholesale_accounts: true` | Required route opt-in; absence or partial support selects legacy only before issuance and never permits fallback afterward |

The receiver RPCs (`FundWholesaleAccount`, `AdmitAuthorization`,
`AdvanceAuthorization`, `SettleAuthorization`, `GetWholesaleAccount`, and
`GetSpendAuthorization`) are broker-to-payee responsibilities. LOC does not
call the payee Unix socket. It uses the broker's authenticated HTTP account and
durable job/session status surfaces from the pinned route.

The payer signature is deterministic protobuf plus Keccak-256 and Ethereum
personal-sign as specified upstream. LOC asks the payer daemon to sign; it does
not duplicate canonicalization or access the payer key.

## Why LOC must change

A workload ceiling answers how much LOC permits a customer engagement to cost.
It does not answer how much new economic value LOC must transfer to an
orchestrator now. The current protocol collapses those quantities, so a large
ceiling can cause LOC to mint a large ticket even when reusable wholesale
credit already exists at that payee.

The target model keeps four quantities distinct:

| Quantity | Owner and purpose |
|---|---|
| Customer maximum | LOC policy: bounds the customer's cumulative debit |
| Wholesale reservation | Payee account state: protects value from concurrent engagements |
| Ticket EV | Wholesale funding: adds only bounded account shortfall |
| Settled wholesale debit | Broker evidence: actual wholesale cost of delivered work |

Customer pricing is a fifth, separate concern. LOC may pass through wholesale
wei, add a fee, apply an independent retail price, or offer multiple plans.
Broker settlement proves wholesale cost; it does not dictate the customer's
price.

## Current assumptions that do not carry forward

The existing implementation and its v1 documentation remain authoritative
until the new protocol ships. The following are compatibility facts, not target
invariants:

| Current v1 assumption | Future contract |
|---|---|
| LOC derives `funded_value_wei` from a job or session ceiling and requests that value from `CreatePayment`. | The ceiling bounds a single-purpose authorization. Funding covers only the bounded aggregate payer-payee shortfall. |
| Residual credited value is discovered and operated through a payment-derived `work_id`. | The stable economic account belongs to `(chain, payer, payee, denomination)`; `work_id` may identify ticket-validation generations but never owns residual credit. |
| A payment/session row's funded amount is both the customer hold and the wholesale funding reference. | LOC customer holds and charges live in a customer ledger; wholesale account funding, reservations, and debits live in a separate ledger and correlate by opaque engagement ID. |
| Job correctness relies on the caller or SDK forwarding settlement, with reconciliation as a fallback. | SDK forwarding is only a latency optimization. LOC independently reconciles durable broker-signed admission and settlement state by the LOC-issued request or session ID. Raw HTTP callers have the same accounting semantics. |
| An extensible session obtains per-session refill tickets, and a bounded session can fund its full maximum at open. | A session maximum is a cumulative authorization cap. The broker consumes bounded runway, while LOC replenishes the shared payer-payee account at an aggregate threshold. |
| Recipient rotation and `work_id` rebind chains protect payment-owned value. | Rotation replaces ticket-validation state without losing or reallocating stable account credit; the engagement authorization and reservation remain explicitly accounted for. |
| A conservative full customer charge can resolve missing settlement because funded wholesale value and customer exposure coincide. | Customer resolution follows LOC pricing policy and verified actual usage. Unknown broker state remains financially closed until the versioned protocol's terminal evidence rules permit release or charge. |

## Authorization boundary

LOC authenticates the customer, evaluates their balance and spend policy,
selects or validates the route, and locks the final route before issuing any
authority. The single-purpose authorization must, at the semantic level, bind:

- exactly one LOC-issued request or session identity;
- the payer, selected payee, and selected broker;
- capability, offering, protocol, immutable quote identity, and work unit;
- a maximum cumulative debit, nonce, and validity window;
- caller proof or narrowly scoped proof of possession;
- a finite-request commitment or a workload-appropriate session commitment;
  and
- a protocol/canonicalization version and LOC signature.

This list states required protections, not field names or serialization.
Implementers must consume the published Modules types rather than translate
these bullets into an LOC-invented schema.

The authority cannot be broadened, replayed for another workload, redirected
to another broker or payee, or used for generic access to LOC's pooled credit.

## Route selection and failover

A customer may provide route constraints or influence selection, but LOC must
resolve those inputs against trusted registry data and persist the final route
before signing. Customer-supplied URLs, prices, payees, quote data, or
settlement keys never become authoritative.

Changing orchestrators requires a new authorization because it changes both
the wholesale account and the price. LOC must keep the original customer and
wholesale reservations until the first authorization is either:

- irrevocably retired by authoritative broker/payee evidence; or
- allowed to continue under its original locked route.

A timeout, caller assertion, transport failure, or absence of an SDK callback
is not proof that the first authorization cannot be spent.

## Jobs and reconciliation

For a finite job, LOC will hold customer exposure according to its pricing
policy and request one exact, route-locked authorization. The payee atomically
admits that authority, applies any shortfall-funding tickets, and reserves no
more than its maximum wholesale debit before broker work begins.

The SDK should continue to invoke the broker and forward signed settlement as
the fast path. Correctness cannot depend on that callback. LOC must poll or
otherwise query durable, broker-signed state using the LOC-issued request ID,
verify it against the immutable route and pricing snapshot, apply customer
pricing to actual usage, and release the customer hold idempotently.

An authorization reported as expired-unused may release reservations only if
the published receiver contract makes that terminal state irrevocable. Silence
or an unverified `NO_RECORD` equivalent is insufficient.

## Long-running sessions

The session maximum is a cumulative authorization cap, not a prepaid ticket
value. LOC may hold the customer maximum according to its plan, but the payee
reserves and debits only bounded runway from the shared wholesale account as
verified usage advances.

Replenishment is account-level policy across all active LOC engagements with
the payee. Conceptually:

```text
target float = active reserved runway + configured safety buffer
shortfall    = max(0, target float - available wholesale credit)
```

LOC evaluates that shortfall only after available credit falls below the
operator-configured `WHOLESALE_REPLENISH_BELOW_WEI` low-water mark; values
between the low-water mark and target remain reusable residual credit and do
not trigger a mint. LOC requests funding only for the bounded shortfall. If replenishment is
unavailable, the broker may work only within already authorized and funded
runway and must wind down without extending involuntary credit. Increasing an
extensible session's cumulative cap requires a new idempotent authorization
revision under the published protocol; it is not a generic refill ticket.

## Ledger separation

LOC will maintain two linked but independent accounting views:

1. **Customer ledger:** tenant balance or plan allowance, holds, retail price
   snapshot, charges, refunds, spend windows, and API-key attribution.
2. **Wholesale ledger:** LOC payer/payee account funding, reservations,
   settled wei debits, adjustments, and reconciliation state.

The views share opaque correlation identities, not balances or spend
authority. A customer cannot observe, claim, or directly spend residual pooled
wholesale credit. The orchestrator does not receive LOC customer identity,
balance, plan, margin, discount, or retail price.

Migration `0025` establishes this boundary without reclassifying legacy rows:
`payment_session.accounting_mode` defaults to `legacy_ticket`; account-aware
engagements persist an immutable `customer-pricing/v1` snapshot and customer
maximum, while `credit_ledger.related_engagement_id` replaces the legacy
payment linkage. Separate `wholesale_account` and `wholesale_funding` tables
contain no customer or API-key ownership columns. Enabling the new route path
still requires the authorization, reconciliation, and rollout beads.

Authorization history is append-only in `spend_authorization_grant`. Each
revision persists its exact locked route, commitment, caller key, payer,
maximum, validity window, and predecessor. Selecting a successor updates the
engagement's current pointer but does not mark the predecessor retired; only
the locked broker's durable authorization status may advance it to admitted,
settled, expired-unused, superseded, or outcome-unknown. LOC polls that status
independently of SDK callbacks. Only settled, expired-unused, and superseded
set the local retirement timestamp; customer billing still requires the signed
workload settlement. LOC does not offer in-place route mutation: a different
orchestrator requires a separately authorized engagement while the original
authorization and hold continue until authoritative terminal evidence arrives.

For wholesale pass-through, LOC may calculate the customer charge from the
verified wholesale debit. For cost-plus or retail plans, LOC applies the
persisted customer price policy to verified actual usage. In every case,
broker-signed settlement remains wholesale evidence rather than a customer
invoice.

## Compatibility and rollout

Until the versioned Modules contract is published and implemented, LOC keeps
the current paid-job/session behavior unchanged. Existing endpoints, payment
rows, `work_id` relationships, and conservative recovery rules must not be
silently reinterpreted as shared-account semantics.

Migration requires:

- explicit protocol and feature negotiation;
- separate adapters and persisted version markers for legacy and new records;
- inventory and safe drain or protocol-defined transfer of legacy residual
  credit, without double credit or stranded redeemable tickets;
- idempotent migration and recovery for in-flight jobs and sessions;
- conformance evidence for authorization scope, atomic reservation, durable
  status, rotation, expiry, and aggregate replenishment; and
- operator limits for target float, maximum float, single-ticket EV,
  reservation/cumulative caps, reconciliation deadlines, and drain behavior.

LOC fails closed if a selected route, broker, sender, or receiver does not
advertise and prove support for the complete new account and authorization
semantics. It must not mix a new authorization with legacy funding/account
behavior. A route may continue under the legacy path only when the caller
explicitly entered a supported legacy protocol version and all legacy
invariants remain intact.

## Protocol-owned details

LOC implementation must consume, not independently decide:

- language-neutral message fields and canonical signature bytes;
- authorization revision, replay, expiry, and irrevocable retirement rules;
- atomic admission/reservation/debit APIs and durable status schemas;
- account identity encoding and authenticated account query mechanisms;
- ticket-generation rotation and legacy residual-credit disposition;
- capability negotiation and exact mixed-version failure behavior; and
- broker evidence needed to retire a route during failover.

Any change to these details requires a new Modules protocol baseline and
regenerated vendored bindings before LOC changes behavior.

## Consequences

- Ticket issuance is no longer a customer billing event by itself.
- Customer maximums remain strict even when wholesale funding is pooled.
- LOC assumes the variance and float exposure of its chosen wholesale funding
  policy, but bounds it per payee and in aggregate.
- SDKs remain useful integrations, not trusted accounting principals.
- Raw HTTP remains a first-class supported invocation path.
- Route choice becomes an authorization decision with explicit retirement
  semantics, not merely a discovery hint.
- LOC gains flexibility to offer pass-through, cost-plus, retail, or multiple
  plans without changing the Livepeer wholesale protocol.
