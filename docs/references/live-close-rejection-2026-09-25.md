# Live close rejection: 2026-09-25

Read-only production investigation of gateway operation
`5f64c6b8-afc3-4eca-8655-8b63690267b5`, LOC session
`d21fde63-5803-43a9-81b4-ddd6b2b479a9`, and broker session
`sess_26fe55e0-1f09-480a-bbc2-91549368dc27`.

## Confirmed rejection

LOC telemetry contains 19 `server.settlement_verification_failed` events
between 11:49:05.456908 and 11:59:21.379568 UTC, all with reason
`settlement_replay`. Gateway logs report corresponding `loc_http_400` retries.
This supersedes the initial hypothesis that `claim_debit_gap` was the first
rejection.

The broker's GET `/v1/settlement/d21fde63-5803-43a9-81b4-ddd6b2b479a9`
returned HTTP 200 and a signed `Livepeer-Settlement` envelope whose payload
omits `settlement_seq` (protobuf default zero). LOC's stored
`last_settlement_seq` is zero. The deployed verifier requires the signed
sequence to be strictly greater than the stored sequence.

Running the deployed `_verify_close_settlement` with that envelope and the
persisted session/grant in a PostgreSQL READ ONLY transaction reproduced
`settlement_verification_failed`, reason `settlement_replay`.

Envelope examined: issued_at `2026-09-25T12:17:10.652951244Z`, decoded envelope
SHA-256 `41b247234ff5215beb2c4c32f13b4a4fe73700b056be080d4e51c651816d4739`.
Deployed settlement_verification module source SHA-256:
`9e67699487e927e6b64462e3697e7f10246d9131bffee10e6080c6b11209066f`.
The retrieved envelope is later than the original retries; historical
telemetry independently establishes their rejection reason.

## Second blocker

The signed terminal record identifies predecessor authorization
`loc-auth:0894ae51-0144-4bcc-84d6-eabb494b0645`, with claimed units 124,
debited/actual/billed units 120, billed value 120000000000000 wei, and
termination reason `authorization_exhausted`.

For a diagnostic only, the session ORM object was detached and its in-memory
last sequence set to -1. The unchanged signed envelope then failed with
`claim_debit_gap`. No database field or signed payload was changed. This
demonstrates a second guard, not a recommended bypass or successful settlement.

The deployed verifier already selects the historical grant named by the
settlement and binds verification to it. The newest authorization pointer is
not the rejection established here.

## Accounting observations

LOC remains open with last sequence zero, no actual units, and both grants
`issued`: predecessor cap120 / 120000000000000 wei and successor
`loc-auth:b35b4f41-44ef-4e21-b5a3-e4d901effd1e` cap180 /
180000000000000 wei.

The broker's read-only authorization status query reports the predecessor
`settled`, actual units120, billed120000000000000 wei, reserved0, released0,
and receiver settlement sequence15, observed at11:48:55.376592 UTC. This
receiver sequence is separate evidence from the zero sequence in the signed
session settlement; do not substitute it into the signed payload.

The successor query returns HTTP200 with state `canceled_unused`, units0,
billed/reserved/released0, sequence0, observed at11:48:23.818459312 UTC.
LOC's deployed `SpendAuthorizationObservation` rejects this response as
malformed: its state contract does not accept `canceled_unused`. This is an
additional reconciliation contract gap. Define and verify that state's
irreversible non-admission semantics before allowing it to release holds.

## Recommended correction

The Modules team should fix terminal settlement publication to emit a valid,
durable session sequence and preserve stable evidence across lookups/retries.
LOC must retain replay protection. LOC also needs narrowly specified support
for authorization exhaustion with claimed units exceeding verified debited
units, charging only the signed debit after all accounting checks pass.

Releasing the remaining customer hold additionally requires authoritative
resolution of any issued successor; a rejected refill or missing receiver
record alone is not irreversible proof that the successor cannot be admitted.
Duplicate close recovery and structured gateway logging of LOC error details
belong in the joint regression coverage.

No production close, refund, funding, cancellation, or database write was
performed. This investigation confirms rejection; it does not resolve the
session or approve weakening verification.

## LOC implementation follow-up

LOC now parses `canceled_unused` and verifies unused retirement against the
pinned receiver account domain and principals before recording it. Close retains
holds until all newer grants are provably unused, accepts bounded signed
`authorization_exhausted` claim/debit gaps, and replays exact successful closes
without repeating accounting. See `docs/RELIABILITY.md` for the contract.

The zero signed session sequence remains rejected. Modules publication and joint
conformance follow-up is tracked as `loc-m7l`; local LOC implementation is
`loc-ftv`. These local changes do not imply a production deployment or recovery
of this incident's still-invalid signed record.
