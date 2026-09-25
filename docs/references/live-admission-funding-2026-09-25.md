# Initial live admission funding failure, September 25

Read-only investigation of LOC session `65278f12-a629-4f6b-baae-24a9ea05a1ff`
and authorization `loc-auth:ddc21037-6cea-4614-aee1-d6f4e87340b9`.

Receiver log at 2026-09-25 13:17:12.903 UTC records revision0 admission with
`reason=INSUFFICIENT_WHOLESALE_CREDIT`, `grpc_code=FailedPrecondition` for these
exact identities. Broker logs at13:17:12.912 instead surface HTTP401
`payment_invalid`, work_units0, repeated on subsequent retries.

LOC persisted an open session at13:17:12.506853 UTC and an issued grant at
13:17:12.541789 UTC, max120 units and max120000000000000 wei. Grant expiry is
September26 at13:17:12.541789 UTC.

Receiver account snapshot (version449, last observed ledger update
13:11:38.532982285 UTC) and LOC's matching wholesale_account row report:

| Field | Wei |
|---|---:|
| Credited | 2968977606000000 |
| Debited | 2922461270000000 |
| Reserved | 0 |
| Available | 46516336000000 |
| Required initial reservation | 120000000000000 |
| Shortfall | 73483664000000 |

This is a snapshot retrieved after the incident, not a historical transaction
trace; the receiver log independently confirms the original credit refusal.
There are no LOC wholesale_funding records for this settlement domain at
inspection. No outstanding reservations appear in the snapshot.

Payer is `0x5ae4e42db3671370a0c25aff451e7482aaec3d0b`, payee
`0xd00354656922168815fcd1e51cbddb9e359e3c7f`, chain42161, denominationwei,
domain `0x69dc65065100d9519496b28b36ab223782c0175712524030429b8e33757a2bba`.
The receiver and grant domains match. This is not a gas-wallet shortfall.

## Funding policy gap

Deployed `_open_wholesale_session` issues/persists authorization and calls
`_replenish_for_request` without an admission reservation requirement.
Deployed `_replenish_wholesale_account` uses these settings:

| Setting | Wei |
|---|---:|
| Target available | 100000000000000 |
| Replenish below | 25000000000000 |
| Maximum single funding | 100000000000000 |
| Maximum available per payee | 2000000000000000 |
| Maximum aggregate available | 3000000000000000 |

Available credit is above the refill trigger, so this policy skips funding.
Even its configured target is below the new live admission reservation. The
ABR admission-readiness fix does not apply to this live-open path.

## Required correction

Extend admission-aware bounded funding and final readiness verification to live
open. Compute the requirement from the original authorization, preserve request
and mint identities across retry, and return a retryable funding error if the
reservation cannot be covered within operator limits. Do not silently shrink
the caller's authorized workload cap. Refills need equivalent treatment using
the receiver's atomic successor reservation rules rather than blindly funding
the entire cumulative cap again. A balance check is not an atomic reservation;
concurrent admission still needs the existing separate concurrency follow-up.

At this snapshot the required top-up is73483664000000 wei, below the configured
single-funding maximum; policy should consider that shortfall even though the
balance is above the low-water threshold. Recovery from a completely empty
account would require120000000000000 wei and exceeds that single-funding limit.

Modules should preserve the insufficient-credit classification at the broker
boundary rather than return401 `payment_invalid`.

The individual authorization-status lookup returned502 during inspection, so
its current receiver state was not established. That response is not evidence
of zero usage or irreversible cancellation. No funding, release, cancellation,
configuration change, deployment, or production database write was performed.

## LOC correction

Implemented under `loc-p68`: initial live admission raises its funding floor to
the signed maximum debit and performs a final receiver readiness check. Refill
uses verified predecessor debit/reservation reuse and repeats its observation
after funding. Operator limits and original request/mint/grant identities remain
unchanged. Tests reproduce the exact available/required amounts above, verify
receipt-loss recovery, reject insufficient limits and concurrent depletion, and
exercise cumulative successor funding. This is a local code correction, not a
production deployment or manual funding of the incident authorization.
