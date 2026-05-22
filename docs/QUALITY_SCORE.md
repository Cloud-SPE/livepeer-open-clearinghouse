# QUALITY_SCORE.md

A per-domain quality grade. Updated as the system evolves. The point is to
know honestly where the rough edges are — not to give every domain an A.

## Rubric

| Grade | Meaning |
|---|---|
| **A** | Fully implemented; well-tested (unit + integration); fail-closed paths exercised; docs current; no known correctness issues |
| **B** | Implemented; tests cover happy path and main failure modes; minor gaps acknowledged |
| **C** | Implemented but incomplete or fragile in places; gaps explicitly tracked in tech-debt-tracker |
| **D** | Stub or partial implementation; known broken or untested edges |
| **F** | Not implemented; placeholder only |

## Current grades

| Domain | Grade | Notes |
|---|---|---|
| `accounts` | F | Not implemented |
| `api_keys` | F | Not implemented |
| `billing` | F | Not implemented |
| `discovery` | F | Not implemented |
| `payments` | F | Not implemented |
| `usage` | F | Not implemented |
| `admin` | F | Not implemented |

## Cross-cutting

| Area | Grade | Notes |
|---|---|---|
| Layered-architecture lint | F | Not implemented; manual enforcement only |
| Integration tests against real daemons | F | No test harness yet |
| Observability (logs + `/metrics`) | F | Not implemented |
| Security review checklist | F | `docs/SECURITY.md` exists; no audit performed |
| Frontend portal | F | Not implemented |
| Frontend admin | F | Not implemented |

## How to use this file

- Update the grade when you ship work that materially changes a domain's
  quality (up or down).
- Don't grade aspirationally. A domain that "should be A" but has no tests
  is a C.
- If you down-grade, write what changed in the "Notes" column.
- This file is a snapshot of *current state*, not a roadmap. Aspirational
  work lives in exec-plans.
