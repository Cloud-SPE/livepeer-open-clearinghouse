# Wholesale migration rehearsal evidence: 2026-09-09

Bead: `loc-1zq.9.4`

The `0024` to `0026` wholesale migration was rehearsed with
`infra/scripts/rehearse-wholesale-postgres-migration.sh` against an isolated
clone of the local production-shaped PostgreSQL dataset. The clone contained
3 users, 49 payments, 49 sessions, 96 customer-ledger entries, 45 settlements,
and both `paid-job/v1` and `paid-session/v1` protocols. Four open sessions and
49 issued payments were explicitly moved to terminal test states in the
isolated source clone to model the mandatory operator drain; no source or live
database row was changed.

Result: pass.

- source revision: `0024`
- destination revision: `0026`
- legacy row-count changes: none
- payment/session category changes: none
- financial-total changes: none
- audit failures: none
- historical sessions classified `legacy_ticket`: 49 of 49
- historical rows with new customer or authorization fields: 0
- historical ledger rows linked to a wholesale engagement: 0
- initialized wholesale accounts, fundings, and authorization grants: 0
- global exposure budget: exactly one row, value 0 wei
- dump duration: 134 ms
- restore duration: 729 ms
- migration duration: 714 ms

The local evidence bundle is
`.artifacts/wholesale-rehearsal-20260909/`. It contains the restored-source
dump and the files below. Production approval must retain an equivalent bundle
in the release evidence store; this local rehearsal is not production approval.

| Artifact | SHA-256 |
|---|---|
| `pre.json` | `b3bd2b21b6cc478217388fb8b440dce78e9f2c950a24a0426b1dc60ea9823442` |
| `post.json` | `8c28dc8bdaf242bc13aec96dfb7e08108074bab9ab32c8e138ad597d44d9c7b6` |
| `comparison.json` | `e7de9b593eb077507d2843065e1a333b1f2892ba6e32052521af692fffe46c84` |
| `alembic-upgrade.log` | `02b61c7acdb18811a5abb05cc595acb0a7dbf2cba2ab4a69565ae1c91b6863d3` |
| `timing.json` | `f2fbfa887858a3e8fd49350fc2f6e7dbb67603ede9d30e459fd6f122d9d8bf28` |
