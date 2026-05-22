# AGENTS.md

**This file is the table of contents, not the encyclopedia.** Keep it short
(~100 lines). Deeper knowledge lives in `docs/` and is the system of record.
If something needs to be true forever, put it in a pillar doc and link from
here, not inline.

## What PymtHouse is

A non-custodial-by-design payment clearinghouse for Livepeer applications.
PymtHouse authenticates app developers, manages their wei-denominated credit
balance, and mints signed Livepeer payment tickets on their behalf — fronted
by a single HTTP API so app devs never touch a signing key.

See [`docs/PRODUCT_SENSE.md`](docs/PRODUCT_SENSE.md) for the product story
and [`docs/DESIGN.md`](docs/DESIGN.md) for the load-bearing design decisions.

## How to work here (read this every time)

1. **Knowledge lives in the repo or it doesn't exist.** If a fact, decision,
   or convention isn't in `docs/` or `src/`, an agent can't see it. Don't rely
   on chat history, Slack threads, or human memory. Write it down here.
2. **Parse at boundaries, don't validate.** Pydantic models on inbound HTTP,
   strict types internally. Never trust a `dict` that crossed a network or
   filesystem boundary without parsing it first.
3. **Strict layering, mechanically enforced.** Each domain in
   `src/pymthouse/domains/<name>/` follows `types → config → repo → service →
   runtime → ui`. Cross-cutting concerns enter through `src/pymthouse/providers/`.
   No skipping layers, no upward imports. See
   [`ARCHITECTURE.md`](ARCHITECTURE.md).
4. **Fail closed on billing.** If credit can't be safely decremented, return
   HTTP 402 / 5xx. Never serve work without recorded payment intent. See
   [`docs/RELIABILITY.md`](docs/RELIABILITY.md).
5. **PymtHouse is the only auth gate.** The daemons (`payment-daemon`,
   `service-registry-daemon`) trust their Unix socket. PymtHouse is the only
   thing between an app dev and a signed ticket. See
   [`docs/SECURITY.md`](docs/SECURITY.md).
6. **Boring tech, latest versions.** FastAPI, SQLAlchemy 2.0 async, Alembic,
   Postgres, Lit, vanilla CSS. No build steps in the frontend. No frameworks
   we'd have to explain.
7. **Plans are first-class.** Anything more than a small change gets an
   exec-plan in `docs/exec-plans/active/`. See
   [`docs/PLANS.md`](docs/PLANS.md).

## Map of the repo

| Where | What |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Domain + layer map, dependency direction rules |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Top-level design principles and load-bearing decisions |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | Frontend conventions (Lit, esm.sh, vanilla CSS, light DOM) |
| [`docs/PLANS.md`](docs/PLANS.md) | How plans work; lightweight vs exec-plan |
| [`docs/PRODUCT_SENSE.md`](docs/PRODUCT_SENSE.md) | Product mission, target users, scope guardrails |
| [`docs/QUALITY_SCORE.md`](docs/QUALITY_SCORE.md) | Per-domain quality grading |
| [`docs/RELIABILITY.md`](docs/RELIABILITY.md) | Fail-closed billing, idempotency, state machines |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Key custody, secrets, auth model |
| [`docs/design-docs/`](docs/design-docs/) | Catalogued design docs; start at `index.md` |
| [`docs/exec-plans/active/`](docs/exec-plans/active/) | Plans currently being executed |
| [`docs/exec-plans/completed/`](docs/exec-plans/completed/) | Historical record of completed plans |
| [`docs/exec-plans/tech-debt-tracker.md`](docs/exec-plans/tech-debt-tracker.md) | Known deferrals and debt |
| [`docs/product-specs/`](docs/product-specs/) | Per-domain product specs; start at `index.md` |
| [`docs/references/`](docs/references/) | External references and daemon API cheatsheets |
| [`docs/generated/`](docs/generated/) | Files generated from code (db-schema, openapi.json) — don't edit by hand |
| `src/pymthouse/` | Python application code (see ARCHITECTURE.md for layout) |
| `web/portal/` | User dashboard SPA (Lit, zero-build) |
| `web/admin/` | Operator console SPA (Lit, zero-build) |
| `migrations/` | Alembic database migrations |
| `tests/` | `unit/`, `integration/`, `e2e/` |

## Domains (MVP)

| Domain | Purpose |
|---|---|
| `accounts` | Self-signup (email/password + Google/GitHub), email verification, operator approval |
| `api_keys` | Per-user multiple keys; shown once; hashed at rest; per-key usage attribution |
| `billing` | Wei-denominated credit pool; topup; auto-replenish; spend-cap windows |
| `discovery` | Thin auth-aware proxy over `service-registry-daemon` (returns raw routes) |
| `payments` | Orchestrates `registry.Select` + `payment-daemon.CreatePayment`; charges EV at issuance |
| `usage` | Per-key usage tally; app-dev-reported reconciliation for variable-cost jobs |
| `admin` | Operator approval, cap setting, manual topup |

## Quick commands

```bash
make dev            # docker compose up (postgres + daemons + pymthouse-gateway)
make down           # docker compose down
make migrate        # alembic upgrade head
make fmt            # ruff format
make lint           # ruff check + mypy
make test           # pytest
make logs           # tail compose logs
```

## When in doubt

- Don't add features the product spec doesn't call for.
- Don't add abstractions until they're earning their keep.
- Don't write comments that explain *what*; write comments that explain *why*.
- Read [`docs/design-docs/core-beliefs.md`](docs/design-docs/core-beliefs.md)
  before reaching for clever ideas.
