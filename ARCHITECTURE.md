# ARCHITECTURE.md

A bird's-eye view of where things live and how they depend on each other.
Optimized for navigability, not exhaustive description. When details matter,
go read the code.

## Bird's-eye view

PymtHouse is a single Python (FastAPI) service that:

1. Authenticates app developers (API key) and operators (session cookie).
2. Manages a per-user wei-denominated credit balance in Postgres.
3. Proxies discovery to `service-registry-daemon` over a Unix socket.
4. Mints Livepeer payment tickets via `payment-daemon` over a Unix socket,
   using a single pooled wallet that `payment-daemon` owns.
5. Serves two Lit-based SPAs (user portal, operator admin) as static files.

It runs as one container (`pymthouse-gateway`) alongside Postgres and the two
Go daemons in a single Docker Compose stack. It is not horizontally scalable
in MVP — the in-process APScheduler and the single pooled wallet make this a
single-instance system.

## Code map

```
src/pymthouse/
├── main.py            # FastAPI app factory, lifespan, route registration
├── settings.py        # Top-level Pydantic Settings — env -> typed config
├── providers/         # Cross-cutting concerns (the "Providers" lane)
│   ├── db/            # SQLAlchemy 2.0 async engine + session
│   ├── auth/          # API key + web session validation
│   ├── email/         # EmailProvider Protocol + Resend impl + NullEmail
│   ├── oauth/         # Google + GitHub OAuth flows
│   ├── payment_daemon/   # gRPC client over UDS to payment-daemon
│   ├── registry_daemon/  # gRPC client over UDS to service-registry-daemon
│   ├── clock/         # Clock Protocol (testable time)
│   └── telemetry/     # Structured JSON logging + Prometheus metrics
└── domains/
    ├── accounts/
    ├── api_keys/
    ├── billing/
    ├── discovery/
    ├── payments/
    ├── usage/
    └── admin/

web/
├── portal/            # User dashboard SPA (Lit, zero-build)
└── admin/             # Operator console SPA (Lit, zero-build)

migrations/            # Alembic versions
tests/                 # unit/, integration/, e2e/
```

## The layering rule

Every domain follows the same shape:

```
domains/<name>/
├── types.py       # Pydantic models — the domain vocabulary
├── config.py      # Domain-specific config (rarely needed; bubbles to settings)
├── repo.py        # SQLAlchemy queries — the only file that touches the DB
├── service.py     # Business logic — pure, takes a session + providers, returns types
├── runtime.py     # FastAPI routes + APScheduler jobs — the only file that touches HTTP
└── ui.py          # (optional) Server-rendered fragments if a domain needs them
```

**Allowed dependency direction:** strictly left-to-right.

```
types → config → repo → service → runtime → ui
```

A `service` may not import from `runtime`. A `repo` may not import from
`service`. Skipping layers is allowed (e.g., `runtime` may use `types`
directly), but reversing direction is not.

**Providers are sibling, not parent.** A `service` may take a Provider
(e.g., `EmailProvider`) as an argument. A Provider may not import from any
domain. This keeps the cross-cutting interfaces clean and reusable.

These rules are intended to be enforced mechanically with a custom lint pass
once the layout is stable. See `docs/exec-plans/tech-debt-tracker.md`.

## Domain index

| Domain | Owns |
|---|---|
| `accounts` | `users`, `user_email_verifications`, `user_oauth_identities`, `operator_approvals` |
| `api_keys` | `api_keys` (hashed at rest, `prefix` for display) |
| `billing` | `credit_balances`, `credit_topups`, `spend_window` ledger, replenishment policy |
| `discovery` | (no tables — pure proxy) cached `ResolveResult` snapshots in v2 |
| `payments` | `payments` (a row per `CreatePayment` call: work_id, recipient, EV, status) |
| `usage` | `usage_records` (idempotent on `(api_key_id, request_id)`), reconciliation deltas |
| `admin` | Aggregates over the above; operator config (default credit grant, period caps) |

## External integration shape

```
┌─────────────────┐     HTTPS      ┌──────────────────────────────────┐
│  app dev / SDK  │ ───────────→ │  pymthouse-gateway (this service) │
└─────────────────┘                └──────────────────────────────────┘
                                              │
                            ┌─────────────────┼─────────────────┐
                       SQL TCP             UDS                UDS
                            │                │                  │
                            ▼                ▼                  ▼
                  ┌────────────────┐  ┌────────────────┐  ┌─────────────────────┐
                  │   Postgres 16  │  │ payment-daemon │  │ service-registry-   │
                  │                │  │   (sender)     │  │     daemon          │
                  │                │  │  + V3 keystore │  │   (resolver)        │
                  └────────────────┘  └────────────────┘  └─────────────────────┘
```

- **Daemons trust their Unix socket** — no token auth on the gRPC surface.
  PymtHouse and the daemons share the `livepeer-run` Docker volume and run
  with matching uid/gid (`65532`).
- **`payment-daemon` is the only thing that holds the wallet key.** PymtHouse
  never sees keystore material. See `docs/SECURITY.md`.
- **`service-registry-daemon.Select()` returns everything `CreatePayment`
  needs** — `eth_address`, `worker_url`, `price`, and the
  `(quote_id, constraint_fingerprint, route_fingerprint)` triplet. PymtHouse
  passes these through without quoting logic of its own.

## The ticket-mint flow (the headline path)

```
POST /v1/payments/mint
  body: { capability, offering, work_units }
  auth: X-API-Key

1. resolve API key                        (providers/auth)
2. read user's credit balance             (domains/billing.repo)
3. registry.Select(capability, offering)  (providers/registry_daemon)
4. compute funding = price × work_units
5. check balance >= funding (fail closed → 402)
6. payment_daemon.CreatePayment(
       recipient = SelectedRoute.eth_address,
       ticket_params_base_url = SelectedRoute.worker_url,
       accepted_price = {capability, offering, price, quote_ref},
       funding = {funded_value_wei = funding, estimated_units = work_units},
   )                                      (providers/payment_daemon)
7. decrement balance by response.expected_value
   record `payments` row (work_id, EV, status=issued)
                                          (domains/payments.service)
8. return { payment_bytes (base64), work_id, expected_value }
```

Each step is in its own layer. Each layer is unit-testable in isolation. The
only network calls are in `providers/` and the only DB writes are in `repo.py`.

## What this architecture is optimized for

- **Agent legibility.** A new agent run can navigate from `AGENTS.md` to the
  exact file it needs to change without reading the whole repo.
- **Mechanical enforcement.** The layering rule can be checked by an import
  linter. Drift is detectable.
- **Single-instance simplicity.** APScheduler in-process, one pooled wallet,
  one Postgres. Horizontal scale is a v2 concern.
- **Single-binary swap.** When `payment-daemon` or `service-registry-daemon`
  publishes a new version, we change one `image:` tag in compose.

## What this architecture is not

- Not a microservice mesh. There is one Python service.
- Not multi-tenant in the sense of multi-issuer. PymtHouse is the sole
  identity issuer for its users.
- Not horizontally scalable in MVP. See `docs/RELIABILITY.md` for the
  reasoning and v2 path.
