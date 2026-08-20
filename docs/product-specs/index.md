# Product Specs

Per-domain product specs. A product spec describes the user-visible
behavior of a domain: the flows, the inputs/outputs, the failure modes,
and the success criteria.

A product spec is not a work item. The spec says "this is what the system will
do." The Beads graph says what must happen to build or change it.

## How to add one

1. Create `docs/product-specs/NNN-short-slug.md`.
2. Add a row to the catalog below with status `draft`.
3. When the corresponding domain is implemented and the behavior matches,
   the spec moves to `shipped`.
4. When user-visible behavior changes, edit the spec in place and bump its
   `Updated` date. Note material changes in a brief "Changelog" section.

## Status legend

- `draft` — being written; not yet a contract
- `accepted` — agreed-upon target behavior; implementation pending
- `shipped` — implemented and live
- `deprecated` — was shipped, now being phased out

## Catalog

| ID | Title | Domain | Status |
|---|---|---|---|
| 001 | Account onboarding | `accounts` | draft |
| 002 | API key lifecycle | `api_keys` | draft |
| 003 | Credit and billing | `billing` | draft |
| 004 | Discovery API | `discovery` | draft |
| 005 | Ticket mint | `payments` | draft |
| 006 | Usage reconciliation | `usage` | draft |
| 007 | Operator admin console | `admin` | draft |

Spec files for each of these will be filled in as part of Phase 2 or as
their domains are implemented. The catalog rows are placeholders so future
agent runs know which specs are expected to exist.
