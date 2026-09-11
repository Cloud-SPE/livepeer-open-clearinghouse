# Production database migration: v1 to v2

This is the operator runbook for the breaking Livepeer Modules v2 cutover. The
source database is never migrated in place during rehearsal. The procedure
takes a consistent dump, restores it into an isolated localhost-only
PostgreSQL container, runs Alembic there, and proves that row counts and
financial totals did not move.

## What the migration does

The supported v1 source revision is Alembic `0013`. The current v2 head is
resolved from the release tree at execution time.

- `0014` drops and recreates `payment_idempotency_key`. Every legacy row would
  be lost, so the preflight refuses any non-empty table.
- `0015` renames `payment_session.mode` to `protocol`. Historical mode values
  are retained for audit but are not valid v2 protocols and are never resumed.
- Later revisions add route, mint, settlement, ticket-validity, rotation, and
  sender evidence. Historical rows remain nullable where those facts cannot be
  reconstructed.

There is no automatic conversion of an open v1 interaction into a v2 one.
Before the dump, stop all writers and explicitly settle or disposition every
`open`/`draining` session and every `reserved`/`issued` payment. The audit
fails closed if any remain.

## Secrets and identities that must survive

Database migration does not replace application identity. Preserve these
values unchanged across the cutover:

- `API_KEY_HASH_PEPPER`, or every existing API key stops authenticating.
- `SESSION_SECRET`, if existing browser sessions should remain valid.
- `WEBHOOK_SIGNING_SEED`, or existing webhook verification secrets change.
- The payer keystore and wallet identity.
- The payer `--db` volume, which contains mint-idempotency state.
- The receiver `--db` and `--txintent-db` volumes.

Both Modules sidecars use `--chain-rpc-urls=<primary>,<fallback>`. Chain-mode
payer startup also requires `--max-payment-wei`; size it above the largest
intended single job with reviewed headroom.

## Rehearse against a quiesced v1 database

Use a read-only database credential. Keep URLs in environment variables rather
than command arguments so credentials do not appear in shell history or the
process command line.

```bash
export SOURCE_DATABASE_URL='postgresql://readonly:...@db.example/loc'
export ARTIFACT_DIR="$PWD/.artifacts/v1-to-v2-$(date -u +%Y%m%dT%H%M%SZ)"
./infra/scripts/rehearse-v1-postgres-migration.sh
```

The command refuses to migrate the restored copy unless all of these are true:

- source revision is exactly `0013`;
- no v1 session is `open` or `draining`;
- no v1 payment is `reserved` or `issued`;
- the legacy idempotency table is empty;
- balances equal the per-user ledger sum;
- balances are non-negative and refunds do not exceed reservations;
- payment/session/settlement references are intact.

Retain `v1.dump`, its SHA-256, `pre.json`, `post.json`, `comparison.json`, the
Alembic log, and `timing.json` as the approval evidence. The measured migration
duration, plus dump/restore time and operational margin, defines the production
maintenance window.

## Production cutover

1. Announce and enter maintenance; stop every LOC gateway and scheduler that
   can write to PostgreSQL.
2. Run the same preflight against the now-quiesced production database and
   take the final custom-format dump. Verify its SHA-256 and restore it once.
3. Run exactly one migration job from the pinned v2 image. Do not let every
   gateway replica race `alembic upgrade head`.
4. Run the post-migration audit and compare it with the pre-migration report.
   Do not start v2 if any failure is reported.
5. Start the v2 sidecars with their persistent stores, then one LOC gateway.
   Verify database revision, health, registry selection, payment minting,
   signed settlement, balance/ledger reconciliation, and idempotent replay.
6. Start remaining replicas, then cut OpenAI and Meetings traffic together.
   Mixed v1/v2 operation is unsupported.

## Rollback boundary

Schema rollback is restore-based, not `alembic downgrade`. If the v2 gate
fails before accepting traffic, stop v2 and restore the final v1 dump with the
complete prior v1 release. If v2 has accepted any traffic, preserve the v2
database and payment-daemon stores for accounting; operator reconciliation is
required before deciding whether restoring v1 is financially safe.

Never run v1 application code against a database migrated past `0013`, and
never represent historical v1 rows as v2 signed settlement evidence.
