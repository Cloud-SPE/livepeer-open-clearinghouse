# ABR receiver funding readiness — September 24, 2026

Investigation and fix: `loc-a4t`. Production inspection was read-only. No funds,
configuration, jobs, authorizations or broker requests were changed or replayed.

## Confirmed incident

Gateway job `19e12b50-c965-42c3-9d42-b86f97d99515` corresponds to LOC job
`3ed8c26e-b98c-4a6c-a0bf-74787bf8d65b` and broker request
`3097957f-85b6-4888-90d5-f0a1baab91df`.

LOC persisted the job at 17:36:47.046399 UTC. Its authorization began at
17:36:47.108836 and expired five minutes later. The EU broker logged HTTP 402,
`insufficient_balance`, zero work units at 17:37:04.078485845 UTC, with repeated
rejections for the same request. LOC obtained signed non-admission and closed
at 17:43:37.694533 UTC: zero units, zero billed, `NOT_ADMITTED`; the grant is
`expired_unused`.

| Quantity | Value |
|---|---:|
| Estimated units | 1,527 video-frame-megapixels |
| Authorized maximum units | 1,000,000 |
| Price | 1,629,000,000,000 wei / 1,000 units |
| Estimate at this price | 2,487,483,000,000 wei |
| Required job reservation | 1,629,000,000,000,000 wei |
| Receiver credited total | 2,968,977,606,000,000 wei |
| Receiver reserved total | 46,000,000,000,000 wei |
| Receiver debited total | 2,357,869,325,000,000 wei |
| Available credit | 565,108,281,000,000 wei |
| Reservation shortfall | 1,063,891,719,000,000 wei |

The matching account observation remains version 363, observed at
14:14:50.432648 UTC. No funding row exists for this job. The deployed code and
current settings confirm that funding was evaluated only against a low-water
threshold of 25,000,000,000,000 wei, with a target and maximum single funding
amount of 100,000,000,000,000 wei each. Available credit exceeds both the
threshold and target, so this path generated no shortfall despite the much
larger admission reservation. The deployed per-payee and aggregate limits are
2,000,000,000,000,000 and 3,000,000,000,000,000 wei respectively.

Job, grant and account coordinates match chain 42161, payer
`0x5ae4e42db3671370a0c25aff451e7482aaec3d0b`, payee
`0xd00354656922168815fcd1e51cbddb9e359e3c7f`, and settlement domain
`0x69dc65065100d9519496b28b36ab223782c0175712524030429b8e33757a2bba`.
This is wholesale ledger credit, not gas-wallet funding.

## LOC correction

For paid jobs, raise the per-attempt replenishment floor to the signed
`max_debit_wei`, and the target to the greater of that reservation and the normal
float target. Reuse the existing locked exposure planner, durable mint identity,
receipt validation and replay logic. Do not raise the operator's single-funding,
per-payee or aggregate limits. Read the receiver account again after funding
completion and require matching account coordinates and sufficient free credit
before returning the authorization to the caller.

With the incident's settings, the required shortfall exceeds the single-funding
limit, so LOC now returns `WHOLESALE_FUNDING_UNVERIFIED` (503) rather than giving
the gateway an unusable authorization. The reason identifies the failed limit.
A signed grant may already exist internally to establish payer identity. It is
not disclosed on this failure; its customer hold remains subject to the existing
idempotent retry and authoritative non-admission recovery rules.

Background float replenishment and live-session reservation policy are unchanged.
The available-balance check is not an atomic admission reservation: credit may
still be consumed after the last observation. Such rejections must continue to
use authoritative non-admission recovery; LOC must not infer refunds from 402.

## Gateway cap decision

The historical gateway code applied the deployment-wide `ABR_MAX_TOTAL_UNITS`
default of 1,000,000 to each job. This persisted job's cap is approximately 655
times its estimate. The estimate uses duration, preset dimensions and a
conservative frame rate; it is not verified actual usage. LOC cannot safely
substitute it for the caller's maximum.

The gateway should derive a per-job ceiling from validated workload bounds and
a documented margin, with the configured maximum as an upper limit. The original
input duration/FPS and complete media probe were not recovered in this read-only
investigation, so 1,527 units is not asserted to be a sufficient final ceiling.
Do not raise production funding limits solely to accommodate the global default.
Gateway working-tree changes were observed during investigation and left untouched;
the deployed behavior must be verified separately.

## Validation

Regressions cover above-low-water shortfalls, unchanged funding caps, sufficient
existing credit, delayed receipt visibility and exact replay identities, and
concurrent reservation consumption between funding acknowledgement and the final
readiness check. Existing job recovery fixtures now provide enough credit for
the reservation they authorize, rather than relying on the old funding gap.
