# AGENTS.md

**This file is the table of contents, not the encyclopedia.** Keep it short
(~100 lines). Deeper knowledge lives in `docs/` and is the system of record.
If something needs to be true forever, put it in a pillar doc and link from
here, not inline.

## What Livepeer Open Clearinghouse is

A non-custodial-by-design payment clearinghouse for Livepeer applications.
Livepeer Open Clearinghouse authenticates app developers, manages their wei-denominated credit
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
   `src/livepeer_open_clearinghouse/domains/<name>/` follows `types → config → repo → service →
   runtime → ui`. Cross-cutting concerns enter through `src/livepeer_open_clearinghouse/providers/`.
   No skipping layers, no upward imports. See
   [`ARCHITECTURE.md`](ARCHITECTURE.md).
4. **Fail closed on billing.** If credit can't be safely decremented, return
   HTTP 402 / 5xx. Never serve work without recorded payment intent. See
   [`docs/RELIABILITY.md`](docs/RELIABILITY.md).
5. **Livepeer Open Clearinghouse is the only auth gate.** The daemons (`payment-daemon`,
   `service-registry-daemon`) trust their Unix socket. Livepeer Open Clearinghouse is the only
   thing between an app dev and a signed ticket. See
   [`docs/SECURITY.md`](docs/SECURITY.md).
6. **Boring tech, latest versions.** FastAPI, SQLAlchemy 2.0 async, Alembic,
   Postgres, Lit, vanilla CSS. No build steps in the frontend. No frameworks
   we'd have to explain.
7. **The work graph is first-class.** Anything more than a small change gets a
   Beads issue before code; substantial work gets an epic with real dependency
   edges. Markdown remains for durable design knowledge, not task tracking. See
   [`docs/PLANS.md`](docs/PLANS.md).

## Map of the repo

| Where | What |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Domain + layer map, dependency direction rules |
| [`docs/DESIGN.md`](docs/DESIGN.md) | Top-level design principles and load-bearing decisions |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | Frontend conventions (Lit, esm.sh, vanilla CSS, light DOM) |
| [`docs/PLANS.md`](docs/PLANS.md) | Beads planning, dependency, and handoff workflow |
| [`docs/PRODUCT_SENSE.md`](docs/PRODUCT_SENSE.md) | Product mission, target users, scope guardrails |
| [`docs/QUALITY_SCORE.md`](docs/QUALITY_SCORE.md) | Per-domain quality grading |
| [`docs/RELIABILITY.md`](docs/RELIABILITY.md) | Fail-closed billing, idempotency, state machines |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Key custody, secrets, auth model |
| [`docs/design-docs/`](docs/design-docs/) | Catalogued design docs; start at `index.md` |
| [`.beads/`](.beads/) | Metadata for the durable work graph; issue data syncs through Dolt |
| [`docs/exec-plans/active/`](docs/exec-plans/active/) | Reserved legacy directory; do not add new trackers |
| [`docs/exec-plans/completed/`](docs/exec-plans/completed/) | Historical implementation narratives |
| [`docs/exec-plans/tech-debt-tracker.md`](docs/exec-plans/tech-debt-tracker.md) | Legacy debt input being migrated under `loc-5vm.3` |
| [`docs/product-specs/`](docs/product-specs/) | Per-domain product specs; start at `index.md` |
| [`docs/references/`](docs/references/) | External references and daemon API cheatsheets |
| [`docs/generated/`](docs/generated/) | Files generated from code (db-schema, openapi.json) — don't edit by hand |
| `src/livepeer_open_clearinghouse/` | Python application code (see ARCHITECTURE.md for layout) |
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
make dev            # docker compose up (postgres + daemons + livepeer-open-clearinghouse-gateway)
make down           # docker compose down
make migrate        # alembic upgrade head
make fmt            # ruff format
make lint           # ruff check + mypy
make test           # pytest
make logs           # tail compose logs
```

## Issue tracking (Beads)

This project uses `bd` as its durable work tracker. The project-local skill at
`.agents/skills/beads/SKILL.md` is the operating manual.

- Run `bd prime` at session start and after context compaction.
- Use `bd ready` to find work and `bd update <id> --claim` before starting it.
- Create a bead with a real description before writing code.
- Record discovered work with `--deps discovered-from:<current-id>`.
- Close completed beads with a reason; do not leave finished work open.
- Never use Markdown checklists, scratch plans, or chat lists as a second task
  tracker, and never run `bd edit`.
- Use `bd dolt push` only when the authority printed by `bd prime` permits it.
- On a fresh clone, follow the tested bootstrap and upgrade procedure in
  [`docs/PLANS.md`](docs/PLANS.md#bootstrapping-beads-on-a-fresh-clone).

## When in doubt

- Don't add features the product spec doesn't call for.
- Don't add abstractions until they're earning their keep.
- Don't write comments that explain *what*; write comments that explain *why*.
- Read [`docs/design-docs/core-beliefs.md`](docs/design-docs/core-beliefs.md)
  before reaching for clever ideas.
