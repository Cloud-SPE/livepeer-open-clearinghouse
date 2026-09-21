# 002. Fair wholesale credit accounts

| | |
|---|---|
| Status | accepted — implemented against the published settlement-domain contract |
| Opened | 2026-09-08 |
| Beads | `loc-1zq` |
| Governing Modules work | `lnm-b41`; `docs/design-docs/wholesale-credit-accounts.md`; plan `0049-fair-wholesale-credit-accounts.md` |
| Integration baseline | Modules commit `80800f8be422b5ed08c6fe65a65611f67131cbfd` (Network Protocol `4.0.0`) |

## Decision

LOC adopts the fair wholesale funding contract implemented by Livepeer
Network Modules. The integration baseline is Modules commit
`80800f8be422b5ed08c6fe65a65611f67131cbfd`: Network Protocol `4.0.0`,
`wholesale-account` `2.0.0-draft`, and spend-authorization signing domain
`livepeer-spend-authorization/v2`. The shared-wallet extension in
[design 003](003-shared-wallet-account-isolation.md) supersedes that account
contract with `3.0.0-draft` and signing domain `v3`. LOC persists the account contract version
and requires the versioned paid protocols rather than inferring support from
daemon image tags or offering metadata.

LOC remains the Livepeer payer and each selected orchestrator remains the
wholesale payee. All LOC customers routed to one independent settlement domain
share LOC's wholesale credit account there. One payee may operate multiple
independent settlement domains, each with its own balance and version. Customer
balances, holds, API-key attribution, spend caps, and pricing remain isolated
inside LOC and never become authority over those accounts.

Wholesale accounts are the only payment model for new work. LOC does not
offer a legacy-ticket mode, rollout toggle, fallback, or offering feature
switch. Authorization-only accounting is intrinsic to the selected
`paid-job/v1` or `paid-session/v1` protocol. Unknown paid protocols are
rejected before LOC creates a customer hold, signs an authorization, or funds
an account.

Tickets will add only a bounded shortfall to the selected settlement-domain
wholesale account.
They will not encode how much an individual customer job or session may spend.
A separate, signed, single-purpose authorization will bound one engagement.
The broker will reserve against that account, debit actual wholesale
usage, and release unused reservation.

This document fixes LOC's responsibilities and migration constraints. It does
not redefine the authorization wire schema, canonical encoding, signatures,
endpoint names, status vocabulary, or negotiation mechanism. Those are owned
by the pinned Modules protocol and vendored protobufs.

### Settlement-domain identity amendment

The Modules team approved this stable account identity:

```text
(chain, payer, payee, settlement_domain_id, denomination)
```

`settlement_domain_id` identifies one independent financial ledger. The
payment-daemon generates it once and persists it with that ledger; the broker
discovers and advertises it. Restoring the complete ledger or changing the
broker hostname preserves the ID. Starting an independent ledger generates a
different ID. A configured ID that conflicts with an existing database must
fail startup and require an explicit migration.

The ID is public metadata, not authority. The cold-signed manifest must bind it
to the orchestrator and broker, and authorizations, account observations, and
settlements must identify it. Sharing an ID never makes independent stores
share balances. A route/domain change requires a new authorization and funding
assessment; existing credit cannot move silently between domains.

Network Protocol `4.0.0` publishes the ID as a non-zero uint256 encoded as
exactly `0x` plus 64 lowercase hexadecimal digits. LOC migration `0028` adds
the internal coordinate and puts pre-v4 `wholesale-account/1.1.0-draft`
records in one explicit compatibility domain for historical audit only. New
traffic must use the authoritative ID advertised in the cold-signed route and
must fail closed on a missing, malformed, zero, or mismatched ID. LOC never
derives an ID from a broker URL, coordinator-local name, or settlement key.

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
| `WholesaleAccountView` | Parsed broker observation persisted with route, settlement domain, version, chain, denomination, and observation time |
| Authorization state and settlement extension fields | Durable reconciliation evidence keyed by LOC authorization plus request/session identity |
| `Livepeer-Authorization` | Base64 signed authorization sent to the locked broker |
| Optional `Livepeer-Payment` | Shortfall funding only on an account-authorized invocation |
| `paid-job/v1` or `paid-session/v1` | Authorization-only accounting is intrinsic; LOC never negotiates it through offering metadata |

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

## Legacy assumptions that do not carry forward

The following describe the retired ticket-per-engagement implementation. They
are historical facts, not supported runtime alternatives:

| Current v1 assumption | Future contract |
|---|---|
| LOC derives `funded_value_wei` from a job or session ceiling and requests that value from `CreatePayment`. | The ceiling bounds a single-purpose authorization. Funding covers only the bounded aggregate payer-payee shortfall. |
| Residual credited value is discovered and operated through a payment-derived `work_id`. | The stable economic account belongs to `(chain, payer, payee, settlement_domain_id, denomination)`; `work_id` may identify ticket-validation generations but never owns residual credit. |
| A payment/session row's funded amount is both the customer hold and the wholesale funding reference. | LOC customer holds and charges live in a customer ledger; wholesale account funding, reservations, and debits live in a separate ledger and correlate by opaque engagement ID. |
| Job correctness relies on the caller or SDK forwarding settlement, with reconciliation as a fallback. | SDK forwarding is only a latency optimization. LOC independently reconciles durable broker-signed admission and settlement state by the LOC-issued request or session ID. Raw HTTP callers have the same accounting semantics. |
| An extensible session obtains per-session refill tickets, and a bounded session can fund its full maximum at open. | A session maximum is a cumulative authorization cap. The broker consumes bounded runway, while LOC replenishes the selected settlement-domain account at an aggregate threshold. |
| Recipient rotation and `work_id` rebind chains protect payment-owned value. | Rotation replaces ticket-validation state without losing or reallocating stable account credit; the engagement authorization and reservation remain explicitly accounted for. |
| A conservative full customer charge can resolve missing settlement because funded wholesale value and customer exposure coincide. | Customer resolution follows LOC pricing policy and verified actual usage. Unknown broker state remains financially closed until the versioned protocol's terminal evidence rules permit release or charge. |

## Authorization boundary

LOC authenticates the customer, evaluates their balance and spend policy,
selects or validates the route, and locks the final route before issuing any
authority. The single-purpose authorization must, at the semantic level, bind:

- exactly one LOC-issued request or session identity;
- the payer, selected payee, selected broker, and settlement domain;
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

Changing orchestrators or settlement domains requires a new authorization and
funding assessment because it changes the wholesale account and may change the
price. LOC must keep the original customer and wholesale reservations until
the first authorization is either:

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
reserves and debits only bounded runway from the selected settlement-domain
wholesale account as
verified usage advances.

Replenishment is account-level policy across all active LOC engagements in the
settlement domain. Operator aggregate exposure remains global, and the
per-payee exposure limit is summed across every domain for that payee so adding
brokers cannot multiply the configured limit. Conceptually:

```text
target available float = expected near-term unreserved demand + safety buffer
shortfall = max(0, target available float - available wholesale credit)
```

Active reservations are already excluded from the broker's available value
and are not added again. LOC polls each distinct active settlement-domain
account on the operator-configured wholesale replenishment cadence. That
authoritative poll is the accounting-correctness path; SDK balance events are
only latency hints and cannot be required for funding.

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
2. **Wholesale ledger:** LOC payer/payee/settlement-domain account funding,
   reservations, settled wei debits, adjustments, and reconciliation state.

The views share opaque correlation identities, not balances or spend
authority. A customer cannot observe, claim, or directly spend residual pooled
wholesale credit. The orchestrator does not receive LOC customer identity,
balance, plan, margin, discount, or retail price.

Migration `0025` establishes this boundary without falsifying history. Closed
historical rows may retain `accounting_mode = legacy_ticket` as immutable audit
data, but no code may use that label to issue, refill, reopen, or authorize
work. New engagements persist an immutable `customer-pricing/v1` snapshot and
customer maximum, while `credit_ledger.related_engagement_id` correlates the
customer ledger without making it own wholesale value. Separate
`wholesale_account` and `wholesale_funding` tables contain no customer or
API-key ownership columns. Migration `0028` adds settlement-domain identity to
wholesale accounts and authorization grants. Historical pre-v4 rows receive
the compatibility identity described above; no new engagement may select it.

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

## Wholesale-only cutover

LOC does not support mixed payment epochs for new work. Existing payment rows,
`work_id` relationships, and closed historical records must not be silently
reinterpreted as shared-account semantics, but they provide no continuing
legacy spend authority.

Migration requires:

- explicit supported-protocol validation without offering feature negotiation;
- an inventory and complete operator-approved drain of every active legacy
  job, session, payment, and idempotency claim before the new release starts;
- preservation of closed historical labels solely for audit and reporting;
- idempotent migration and recovery for in-flight jobs and sessions;
- conformance evidence for authorization scope, atomic reservation, durable
  status, revision, expiry, and aggregate replenishment; and
- operator limits for target float, maximum float, single-ticket EV,
  reservation/cumulative caps, reconciliation deadlines, and drain behavior.

LOC fails closed if a selected route, broker, sender, or receiver does not
advertise and prove support for the complete account and authorization
semantics. There is no caller-selectable legacy version and no fallback after
route selection. Startup or cutover preflight must reject active legacy state
rather than strand it or revive the retired path.

## Protocol-owned details

LOC implementation must consume, not independently decide:

- language-neutral message fields and canonical signature bytes;
- authorization revision, replay, expiry, and irrevocable retirement rules;
- atomic admission/reservation/debit APIs and durable status schemas;
- account identity encoding and authenticated account query mechanisms;
- ticket-generation rotation and pre-cutover residual-credit disposition;
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

Shared-wallet deployment extends the account coordinate with `wholesale_account_id`; see [account isolation](003-shared-wallet-account-isolation.md) for receipt semantics and migration requirements.
