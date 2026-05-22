# Design Docs

Catalog of design docs. Design docs are durable artifacts that capture
*why* something is the way it is, separate from the *what* (which lives in
the pillar docs and the code).

Use a design doc for: a non-obvious decision where the alternatives matter,
a comparison between approaches, a rationale that a future agent will need
to reconstruct your reasoning.

## How to add one

1. Create `docs/design-docs/NNN-short-slug.md` (sequentially numbered).
2. Add a row to the table below with status `draft`.
3. Write it. Move to `accepted` when the decision is final.
4. If superseded, update the table to `superseded` and link the replacement.

## Status legend

- `draft` — being written or discussed
- `accepted` — current behavior of the system
- `superseded` — was accepted, now replaced by another doc (linked)
- `rejected` — considered and explicitly not adopted (preserved for the record)

## Catalog

| ID | Title | Status | Owner | Notes |
|---|---|---|---|---|
| 000 | [Core beliefs](core-beliefs.md) | accepted | — | Agent-first operating principles for this repo |

(more entries appear here as we accumulate decisions)
