# ABR admission rejection recovery — September 24, 2026

## Assessment

The video gateway reported operation `7b470fbf-3883-4e41-9c16-cd535ecb3bc3`,
LOC job `48697cf8-ffbf-48ff-ba48-bddd9011a241`, and broker request
`a20870eb-9ce9-47a8-b4e8-fe082be46528`, created at 14:20:42 UTC. Reported LOC
and video gateway tags were `v2.0.0`; tags alone do not identify deployed source.

A read-only GET during this investigation confirmed that
`https://eu-central-broker.xode.app/v1/exchange/a20870eb-9ce9-47a8-b4e8-fe082be46528`
returned HTTP 200, `outcome=ADMISSION_REJECTED`, `state=payment_rejected`, and
embedded `status=402`, with broker job ID
`job_eca55218-c79b-4e0f-b419-75ae8b1e731d`. It requested signed non-admission
after authorization expiry. No production authorization, workload, funding,
non-admission request, or accounting mutation was issued during this probe.

LOC had two recovery gaps: its strict outcome enum rejected the diagnostic,
and its existing signed non-admission handler retained proof only as audit.
Recognizing the enum alone would therefore still leave the hold open.

## Recovery change

The reconciler recognizes `ADMISSION_REJECTED`. It waits for the persisted
grant's expiry, then requests proof from the pinned broker under the original
request and authorization identities. Both this diagnostic and `NO_RECORD`
use the same recovery path. Temporary broker/receiver failures retain the hold.

Only delegated signed proof matching the persisted request, authorization,
payer/payee, quote, route, and settlement domain can authorize release. The
proof observation must not predate authorization expiry; the grant must not
already be known admitted. The broker's wholesale contract requires fencing
the exact unused authorization before signing non-admission. LOC serializes
terminal accounting on the job row and closes at zero units/charge, marks the
grant `expired_unused`, records proof, and releases the customer hold once.
No new mint or execution is part of recovery.

The existing status schema remains compatible: `state=closed`,
`accounting_outcome=broker_settled`, `broker_exchange_outcome=NOT_ADMITTED`,
`actual_units=0`, and `billed_value_wei=0`. Here `broker_settled` denotes
terminal broker-evidenced accounting, not successful execution. The close
record has `outcome=NOT_ADMITTED`. Consumers must inspect terminal state and
broker outcome rather than treating all closed jobs as successful media jobs.

## Funding findings and limits

The local Modules broker's `internal/server/middleware/payment.go` requests
`Reservation = authorization.max_debit_wei` for paid jobs. Its definitive
insufficient-balance result creates the rejection tombstone. The receiver's
`internal/store/wholesale.go` compares that reservation against available
account credit, after existing reservations/debits.

| Reported setting | Wei |
|---|---:|
| ABR authorization maximum | 1,629,000,000,000,000 |
| Payer maximum authorization | 4,000,000,000,000,000 |
| LOC account target | 100,000,000,000,000 |
| LOC maximum single funding | 100,000,000,000,000 |

The authorization fits the payer signing ceiling but is 16.29 times the
account target. If available credit at admission equalled that target, the
reservation would be short by 1,529,000,000,000,000 wei. This is a conditional
calculation, not a reconstruction of the account. Existing credit, concurrent
reservations, realized ticket credit, failed funding, and domain identity can
change the actual balance. Successful signing proves neither broker admission
nor sufficient available wholesale credit.

Read-only production inspection on the operator-provided infrastructure and
EU broker hosts established more than the configuration values alone:

- At **14:20:42.606900911 UTC**, the EU broker logged this exact request with
  `status=402`, `livepeer_error=insufficient_balance`, and
  `outcome=insufficient_balance`. Repeated delivery of the same request also
  returned that result. These unsigned diagnostics explain the failure;
  they are still not refund authority.
- LOC's job and grant both authorize **1,000,000 units**, at
  **1,629,000,000,000 wei per 1,000 units**, giving the reported
  **1,629,000,000,000,000 wei** maximum. The workload estimate was only
  **1,527 units**, or **2,487,483,000,000 wei** at that quote. Admission reserves
  the maximum, not the estimate.
- LOC's account observation and the broker's account endpoint agree at
  **version 363**, with account timestamp **14:14:50.432648 UTC**:

  | Account total | Wei |
  |---|---:|
  | Credited | 2,968,977,606,000,000 |
  | Reserved | 46,000,000,000,000 |
  | Debited | 2,357,869,325,000,000 |
  | Available | 565,108,281,000,000 |

  Available credit is **1,063,891,719,000,000 wei below** the requested job
  reservation. This persisted account version predates admission and remained
  the version returned during investigation; it is consistent with the
  definitive insufficient-balance diagnostic. A per-attempt receiver error
  containing its numeric balance was not present in the inspected logs.
- Current LOC low-water is **25,000,000,000,000 wei**. Available credit exceeds
  both that threshold and the **100,000,000,000,000 wei** target, so the
  shortfall policy does not request funding for this job. No LOC
  `wholesale_funding` records were found for the matching account. This does
  not imply its existing credit was never funded: the receiver reports the
  credited total above, whose historical funding provenance was not recovered.
- The job route, grant, LOC account, and broker account all match chain
  `42161`, denomination `wei`, payer
  `0x5ae4e42db3671370a0c25aff451e7482aaec3d0b`, payee
  `0xd00354656922168815fcd1e51cbddb9e359e3c7f`, and settlement domain
  `0x69dc65065100d9519496b28b36ab223782c0175712524030429b8e33757a2bba`.
  No domain mismatch was found in these records.

The evidence supports **an oversized job reservation relative to available
wholesale credit**, followed by the independent LOC recovery parsing gap.
The payer's authorization limit allowed signing; it did not guarantee receiver
funds. Raising only `WHOLESALE_MAX_SINGLE_FUNDING_WEI` would not fix this:
the current balance is above the replenishment threshold, so no mint is
requested. Future admission policy must align workload maxima with available
reservation capacity or deliberately resize float and its low-water policy.

During inspection, an independent operator action had already resolved the job:
at **15:00:09.744719 UTC**, LOC closed it as `operator_refund_hold`, with a single
**1,629,000,000,000,000 wei** `engagement_release` ledger entry and grant state
`operator_resolved`. This investigation did not perform that action. Do not
reopen it or issue another release. The new reconciler ignores terminal jobs
and rechecks state under lock if an operator resolves one during lookup.

Observed deployment identities:

| Component | Repository digest / revision |
|---|---|
| LOC | `sha256:5ce07d15fb3686d085ea212061d6c52d20a94b01fe18062795d0fb3cbacd5b3d`; OCI revision `401c2e80171645152b19fc7dcba705def3bae5b7` |
| Video gateway | `sha256:0889097d518a7c80fc8db5673c7368b64fed12824246f189e31b0cf3deb541d0` |
| Payer | `sha256:8260bb70f5cb55a471287c90fe78a8f26d4cf6983d24b777e4042ae805d6a9ca` |
| EU broker | `sha256:4f4c0a81bb2ecfe1b59377a278ed65ae884ba7d6c50540bf3dcd4b62ceef53ae` |
| EU receiver | `sha256:12b7fb1fab424d431037615128b259b906c79a47f1172685f5a598a2538c5100` |

The other images did not expose an OCI source revision. Funding investigation
is tracked as `loc-ml0`; recovery implementation is `loc-sy3`.

For future incidents or deeper historical funding attribution, collect a
read-only export scoped to the existing job/request and its account:

1. LOC's job route snapshot and original `spend_authorization_grant`: IDs,
   payer/payee, chain, denomination, domain, quote, maximum, and expiry. Omit
   credentials and executable authorization bytes from shared reports.
2. Matching `wholesale_account` observation and `wholesale_funding` records:
   target, shortfall, mint ID, status, broker acknowledgement, credited amount,
   account version, and timestamps. Include receipts around 14:20:42 UTC;
   a latest balance alone cannot explain the earlier refusal.
3. Broker/receiver admission logs with `available` and `required`, plus
   credited/reserved/debited totals and any concurrent reservations in the
   same account. Verify `available = credited - reserved - debited`.
4. Compare the complete `(chain, payer, payee, settlement_domain_id,
   denomination)` tuple across the locked route, grant, funding receipt, and
   receiver ledger. Identify deployed image digests/source revisions.

Do not increase limits merely to clear this existing operation: the rejection
tombstone must recover under the same identity and cannot re-enter execution.
For future jobs, reduce the allowed maximum or deliberately budget sufficient
available float for full job reservations and concurrency. See the
[tuning guide](../PRICING_AND_CAPABILITY_TUNING.md).

## Rollout verification

Deploy the reviewed LOC change through the normal release process. No database
migration or data reset is needed. Keep scheduler reconciliation enabled and
preserve all original IDs and durable stores. For an unresolved job after expiry,
confirm the broker
can fence the original authorization and return signed non-admission, then
observe the job's terminal status and exactly one customer release. Repeat
status polling to confirm no additional release or mint. Confirm the video
gateway maps this terminal non-admission to a failed operation rather than
successful output or indefinite processing.

The local regression uses real HTTP boundary parsing, signed evidence, and a
database-backed job lifecycle with a temporary proof-endpoint failure. It also
checks early evidence, unexpired grants, mismatched scope/domain, tampering,
and repeated reconciliation, including an operator resolution during lookup.
Automatic recovery of other unresolved jobs still requires deployment and the
original broker's authoritative evidence. The incident job above was already
resolved by an operator and must remain closed.

Validation for the local change: `make check` passed; all 487 backend tests
passed with 110 pre-existing legacy skips. The documented separate Python
conformance invocation passed all three cases with two upstream WebSocket
deprecation warnings. Combining backend and conformance paths into one pytest
invocation selects the backend warning-as-error policy and prevents the mock
server from starting; use the separate commands in [TESTING.md](../TESTING.md).
The new tests require no live broker and do not mutate production.
