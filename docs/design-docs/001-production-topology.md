# 001. Production topology and security baseline

**Status:** accepted  
**Opened:** 2026-09-05  
**Decision owner:** LOC operator

## Context

The first Modules v2 release must preserve payment correctness before it adds
horizontal scale. LOC currently runs its reconciliation, idempotency cleanup,
deposit snapshots, auto-replenishment, and telemetry retention schedulers
inside the gateway process. Its rate limiters are also process-local. Running
multiple gateway processes would therefore duplicate schedulers and make
rate-limit behavior replica-dependent.

The deployment platform and PostgreSQL vendor are operator choices. This
decision defines the invariants that any chosen platform must satisfy.

## Decision

Deploy one pinned v2 release unit with exactly one LOC gateway process and one
Uvicorn worker. The unit also contains one sender-mode payment daemon and one
resolver-mode service-registry daemon. PostgreSQL may be managed or
self-hosted, but is a separate private dependency.

Only the TLS ingress is public:

```text
OpenAI / Meetings / browsers
              │ HTTPS :443
              ▼
       TLS ingress / proxy
              │ private HTTP
              ▼
       one LOC gateway process ───────────────► broker HTTPS APIs
          │       │       │                    email / OAuth providers
          │       │       └── registry UDS ──► Arbitrum RPC endpoints
          │       └────────── payer UDS ─────► Arbitrum RPC endpoints
          │                         │
          │                         └── read-only V3 keystore
          ▼
     private PostgreSQL
```

OpenAI and Meetings integrate only with the LOC HTTPS API. They do not receive
database, daemon, Unix-socket, chain-RPC, or keystore access.

## Component ownership

| Component | Owner | Production invariant |
|---|---|---|
| TLS ingress and DNS | LOC operator | Public port 443 only; modern TLS; forwarded headers restricted to the trusted proxy |
| LOC gateway | LOC operator | Exactly one process/worker; immutable image digest; `APP_ENV=prod`; graceful termination |
| PostgreSQL | LOC operator/database provider | Private network only; encrypted transport and storage; point-in-time recovery; migration role separated from runtime role |
| Payment daemon | LOC operator | Sender mode over UDS only; immutable image digest; dedicated durable `--db`; payment ceiling enforced |
| Service registry daemon | LOC operator | Resolver mode over UDS only; immutable image digest; durable cache/state where configured |
| Payer wallet and keystore | LOC financial operator | Funded policy reviewed; keystore/password readable only by payment daemon |
| Chain RPC | LOC operator/RPC provider | At least two ordered Arbitrum endpoints; authenticated operator-owned service preferred |
| Broker route and settlement keys | Broker operators, verified by LOC | Registry quote and delegated settlement key are snapshotted and verified before accounting |
| Backups, metrics, logs, alerts | LOC operator | Stored outside the runtime host and tested through restore/recovery exercises |

## Network and filesystem boundaries

- Expose only the ingress. Gateway port 8000, PostgreSQL 5432, and daemon
  sockets remain private.
- The gateway and daemons share only the UDS directory. Run them as the same
  dedicated uid/gid (`65532`) and restrict the directory and sockets to that
  identity.
- The payer keystore and password are mounted read-only into the payment
  daemon. They are not mounted into the gateway or registry daemon.
- PostgreSQL uses separate runtime and migration credentials. The runtime role
  can read and mutate application tables but cannot alter schema. The
  migration role is used by one explicit migration job and then removed.
- The gateway needs outbound HTTPS to selected brokers, email/OAuth providers,
  and no arbitrary inbound access around the proxy. Both daemons need outbound
  access to the configured chain RPC endpoints.

## Persistence and recovery objectives

| State | Required persistence | Launch objective |
|---|---|---|
| PostgreSQL | Durable volume/service, continuous WAL/PITR plus daily full backup | RPO ≤ 5 minutes; RTO ≤ 60 minutes; quarterly restore proof |
| Payer `sessions.db` | Durable low-latency filesystem with crash-consistent snapshots | RPO 0 for acknowledged mints; RTO ≤ 30 minutes |
| Registry cache/state | Durable filesystem if configured; otherwise reconstruct from signed sources | RTO ≤ 30 minutes |
| UDS files | Ephemeral shared runtime directory | Recreated on process start; never restored from backup |
| Gateway container | None | Replace from pinned digest; RTO ≤ 15 minutes |
| Logs/metrics/audit evidence | External append/retention service | At least the billing dispute and broker-evidence recovery window |

The payer database cannot be restored casually from an older backup. A stale
copy may forget a mint result while its signed envelope still exists. Preserve
the failed volume for investigation and recover it together with PostgreSQL
under an accounting-aware runbook. The PostgreSQL migration rollback is also
restore-based; see
[`production-database-migration.md`](../references/production-database-migration.md).

## Secrets and configuration

Inject secrets at runtime from the selected platform's secret manager. No
secret belongs in an image, deployment manifest, log, or command argument.

Preserve `API_KEY_HASH_PEPPER`, `SESSION_SECRET`, and
`WEBHOOK_SIGNING_SEED` across the v1-to-v2 cutover. Provision separate
production values for `DATABASE_URL`, `METRICS_TOKEN`, OAuth credentials,
email API/webhook credentials, SDK-manifest signing key, admin bootstrap token,
and payer keystore password.

Both sidecars receive the same ordered
`--chain-rpc-urls=<primary>,<fallback>`. The payer additionally requires:

- `--max-payment-wei`, reviewed against the largest supported single funding
  ceiling plus intentional headroom;
- `--db` on dedicated durable storage;
- the V3 keystore and password file on a payer-only read-only mount.

Set both gateway daemon modes to `grpc`; mock mode is forbidden in production.
Keep `JOB_CONSERVATIVE_CHARGE_AFTER_SECONDS=0` unless the financial operator
has explicitly approved a nonzero conservative-charge policy.

## Deployment and migration boundary

The production image contains Alembic, but schema migration has one owner. Run
`alembic upgrade head` once as an observable pre-deployment job using the
migration credential. Gateway startup then checks schema compatibility and
starts the application without racing another migration actor. The current
development image command still performs an automatic migration, so the
production manifest must override it until the dedicated production entrypoint
lands under `loc-m7s.10.9.4`.

Deployment order is PostgreSQL restore/preflight, migration job, payer and
registry daemons, one gateway, signed canary traffic, then client cutover.
Rollback restores the complete v1 unit and its final pre-migration database;
mixed v1/v2 operation is unsupported.

## Go-live blockers

Production is blocked until all of the following are evidenced:

- actual v1 PostgreSQL dump restores, migrates, and reconciles with no audit
  failures;
- every old open/draining session and reserved/issued payment has an approved
  disposition, and the legacy idempotency table is empty;
- gateway and Modules images are pinned by immutable digest with SBOM and
  provenance;
- one migration actor and a schema compatibility guard replace automatic
  per-gateway migration;
- no production service accepts a development default or mock daemon mode;
- database and daemon interfaces are private and UDS permissions are tested;
- payer, registry, and PostgreSQL persistence survives process/host restart;
- backup and restore tests meet the objectives above;
- wallet funding, `--max-payment-wei`, spend caps, and conservative-charge
  policy are approved by the financial operator;
- signed route selection, job settlement, session close/refill/rotation,
  request-ID recovery, and balance/ledger reconciliation pass against real
  processes;
- OpenAI and Meetings pass their production-shaped conformance flows;
- metrics scraping, structured logs, and alerts work without exposing tokens;
- the final maintenance window and whole-release rollback are rehearsed.

## Alternatives considered

### Multiple gateway replicas

Rejected for the first release. Scheduler ownership, distributed rate limits,
and concurrency behavior must be designed and tested before scaling out.

### Public PostgreSQL or daemon TCP APIs

Rejected. PostgreSQL is private and authenticated; daemons remain UDS-only and
filesystem-trusted.

### Development Compose unchanged

Rejected. It publishes PostgreSQL, supplies development secret defaults,
permits mock daemon modes, uses mutable tags, and runs Alembic in every gateway
startup.

### Platform-specific manifests in this decision

Deferred to provisioning. The operator owns the runtime location; the security
and recovery invariants above are portable and must be enforced by whichever
platform is selected.
