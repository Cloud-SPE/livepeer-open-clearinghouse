# Fair wholesale account rollout and rollback

This is the operator runbook for cutting LOC over from retired
per-engagement tickets to the mandatory Modules `wholesale-account`
`1.0.0-draft` contract pinned in
[the governing design](../design-docs/002-fair-wholesale-credit-accounts.md).
There is no runtime feature toggle or legacy fallback.

## Non-negotiable boundaries

- Closed historical rows remain `accounting_mode = legacy_ticket`. Never
  relabel them, attach a new authorization, or move their value into a pooled
  account. Active legacy rows block cutover.
- Every new engagement uses wholesale semantics. The selected route must
  advertise complete wholesale support and the payer, broker, receiver, and
  LOC release must implement the pinned contract. Partial or unknown support
  fails closed.
- LOC customer holds, pricing, caps, API-key attribution, and charges remain in
  the customer ledger. Broker account credit and debit remain in the wholesale
  ledger. The opaque engagement correlation is not an ownership edge.
- Rollback is a whole-unit release rollback. Do not run an older gateway
  against a newer database, and do not downgrade a database that has accepted
  wholesale traffic.

## Phase 0: inventory and drain

Keep the old release running only long enough to drain all legacy work. Do not
start the wholesale-only release during this phase.

1. Pin the LOC gateway and all Modules images by immutable digest. Record the
   payer address, chain ID, daemon persistent-volume identities, and Modules
   protocol baseline.
2. Inventory every legacy `open`/`draining` session and `reserved`/`issued`
   payment. Settle, expire, refund, or record an approved manual disposition.
   There is no implicit residual-credit transfer.
3. Stop gateways and reconcilers so PostgreSQL is quiescent. Preserve the LOC
   secrets, payer keystore, payer database, receiver databases, and registry
   database as one recovery unit.
4. Run the preflight rehearsal below. Any active legacy engagement, unexpected
   source revision, changed financial total, or changed row/category count is
   a blocker.

## Phase 1: rehearse migration 0024 to head

Use a read-only source credential and a new artifact directory. The script
takes a consistent dump, restores it to an isolated localhost-only Postgres,
requires source revision `0024`, runs Alembic, and compares the restored data.

```bash
export SOURCE_DATABASE_URL='postgresql://readonly:...@db.example/loc'
export ARTIFACT_DIR="$PWD/.artifacts/wholesale-$(date -u +%Y%m%dT%H%M%SZ)"
make migrate-wholesale-rehearse
```

The post-migration audit proves:

- legacy table row counts, payment/session states and protocols, and financial
  totals did not change;
- every historical session has `accounting_mode = legacy_ticket`, while the
  database default for every future session is `wholesale_account`, with new
  customer-pricing and authorization fields unset;
- historical customer-ledger rows were not linked to wholesale engagements;
- wholesale account, funding, and authorization tables are empty; and
- the global exposure budget is the single zero-valued row inserted by the
  migration.

Retain `pre-wholesale.dump`, its SHA-256 and restore listing, `pre.json`,
`post.json`, `comparison.json`, `alembic-upgrade.log`, and `timing.json` in the
release evidence store. A passing rehearsal is necessary but does not enable
the feature.

The repository retains a sanitized summary of the first production-shaped
rehearsal in
[`wholesale-migration-rehearsal-2026-09-09.md`](wholesale-migration-rehearsal-2026-09-09.md).

## Phase 2: deploy wholesale-only

Deploy in this order after the drain preflight passes:

1. Run one migration actor against the quiesced production database.
2. Start the updated payment daemon, then brokers/receivers, then the registry
   daemon. Peers without the mandatory wholesale contract are ineligible.
3. Start one LOC gateway and verify exact schema revision, payer identity,
   registry health, and operator access to `/v1/admin/wholesale`.
4. Run wholesale canaries and confirm authorization, bounded shortfall, broker
   account debit, and customer-ledger reconciliation.
5. Start remaining gateways. Do not mix old and wholesale-only LOC replicas.

## Phase 3: negotiate and initialize

Before starting LOC, record approved values for:

- `WHOLESALE_CHAIN_ID`;
- target and replenish-below float;
- maximum available wei per payee;
- maximum aggregate available wei; and
- maximum single funding wei.

For each candidate payee, force a fresh registry resolution and require the
wholesale feature marker and exact supported contract. Query the authenticated
account using LOC's payer identity and confirm denomination, chain, payer,
payee, broker route, version monotonicity, and non-negative account fields.
Reject a missing feature marker, unsupported version, changed payer/payee,
unauthenticated observation, stale observation, or backwards version.

Start with zero projected exposure. Do not seed the budget from customer holds
or legacy ticket values. Enable one gateway and one allowlisted payee first.
The first funding must equal only the bounded shortfall to the configured
target, never a job or session maximum.

## Phase 4: acceptance gates

Send authenticated raw-HTTP and official-SDK canaries for one job and one
long-running session. The gate passes only when all of these hold:

- each authorization is single-purpose, route-locked, caller-bound, bounded,
  and persisted before work; an orchestrator change creates a new engagement;
- the SDK and raw HTTP paths produce equivalent durable LOC and broker state;
- retrying a request ID does not duplicate a hold, authorization, funding,
  reservation, debit, or customer charge;
- the broker's signed status and settlement can reconcile the engagement with
  no SDK callback;
- the session maximum is cumulative authority while funded account runway is
  bounded independently;
- aggregate and per-payee limits reject concurrent excess funding; and
- customer charges follow the persisted LOC pricing policy applied to verified
  actual usage, not ticket EV or an unverified callback.

Watch the Wholesale admin tab throughout the canary. Stop if projected
exposure exceeds the configured limit, an account observation is stale for
five minutes, a funding remains claimed/minted for five minutes, or broker and
LOC account versions or totals diverge.

## Recovery by durable funding state

| LOC state | Recovery action |
|---|---|
| `claimed` without payment bytes | Do not mint under a different request ID. Re-run the original idempotent funding operation after confirming account version and limits. |
| `minted` with payment bytes | Replay only those exact bytes with the locked broker and request ID, then query authenticated account state. |
| `acknowledged` | Never fund again for that request ID. Reconcile the persisted account version and credited value. |
| Unknown, stale, or contradictory broker state | Stop new wholesale admissions for the payee, preserve customer holds and all evidence, and escalate for operator reconciliation. |

Never delete a funding row, manufacture an acknowledgement, use a customer's
maximum as replacement funding, or assign residual pooled credit to a
`work_id`.

## Rollback decision

Before wholesale traffic, rollback may restore the final `0024` dump together
with the prior complete release and daemon stores.

After any wholesale authorization or funding attempt, disable new admissions
and preserve the current PostgreSQL and daemon stores. Drain and reconcile all
wholesale engagements first. Roll back only to a release that understands the
persisted schema and `accounting_mode`; otherwise use the final pre-enable
snapshot only after a financial operator proves there is no live authorization,
reservation, replayable funding, unsettled debit, or customer hold that would
be lost. A database-only restore or `alembic downgrade` is forbidden.

Do not re-enable legacy traffic. Routes whose wholesale support is absent or
uncertain remain failed closed; rollback restores the complete pre-cutover
release and state only when the financial proof above permits it.
