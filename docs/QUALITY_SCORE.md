# QUALITY_SCORE.md

A per-domain quality grade. Updated as the system evolves. The point is to
know honestly where the rough edges are — not to give every domain an A.

## Rubric

| Grade | Meaning |
|---|---|
| **A** | Fully implemented; well-tested (unit + integration); fail-closed paths exercised; docs current; no known correctness issues |
| **B** | Implemented; tests cover happy path and main failure modes; minor gaps acknowledged |
| **C** | Implemented but incomplete or fragile in places; gaps explicitly tracked in Beads |
| **D** | Stub or partial implementation; known broken or untested edges |
| **F** | Not implemented; placeholder only |

## Current grades

| Domain | Grade | Notes |
|---|---|---|
| `accounts` | B+ | Signup, email verification (incl. self-service + admin resend), login (cookie session), password reset, OAuth (Google + GitHub, find-or-link), rate-limited login/signup/reset. Tested. Missing: linked-identities portal UI; OIDC-as-issuer (tracked). |
| `api_keys` | B | Create / list / revoke; hashed at rest with pepper; prefix-for-display. Tested. Per-key credit isolation is a tracked deferral. |
| `billing` | B+ | Credit balance + ledger + topups; spend-window enforcement with per-user `auto_replenish_threshold_wei` + `auto_replenish_increment_wei`; proactive scheduler + reactive on-mint path; per-user billing config override. 9 unit tests. Missing: per-period replenish cap (tracked); operator audit attribution on auto-replenish ledger rows (tracked). |
| `discovery` | B | `/v1/capabilities`, `/v1/orchestrators`, `/v1/routes` proxied via service-registry-daemon; in-process TTL cache with 7 tests; admin proxy at `/v1/admin/discovery/*`. Composite session-or-API-key auth dep. Missing: orch liveness probing; multi-instance cache (both tracked). |
| `payments` | B | Mint over UDS to payment-daemon (real signer, chain mode); `(api_key_id, idempotency_key)` lookup; deposit snapshot poller every 5 min; Prometheus gauges. Missing: per-ticket redemption attribution (tracked). |
| `usage` | B | `POST /v1/usage/report` for over-committed refund; idempotency on `(api_key_id, request_id)`. Light tests. |
| `admin` | B | Operator approve / approve-without-verification / resend-verification / topup / billing-config; pending list; audit log; deposit snapshot view; discovery proxy. Missing: operator CRUD + role separation, 2FA (all tracked). |

## Cross-cutting

| Area | Grade | Notes |
|---|---|---|
| Layered-architecture lint | A | `scripts/check_layering.py` enforces types→config→repo→service→runtime→ui per domain + providers/ rules; runs clean. |
| Integration tests against real daemons | C | Phase-13 GrpcPaymentDaemonClient tested against the real `tztcloud/livepeer-payment-daemon:v1.3.0` image and confirmed end-to-end (alice signed up, got approved, minted a 784-byte ticket signed by the keystore wallet to a live orch with EV charged). No automated harness. |
| Observability (logs + `/metrics`) | C | structlog JSON logs throughout; Prometheus `/metrics` gated by `METRICS_TOKEN`; per-route + per-status counters; deposit gauges; `livepeer_open_clearinghouse_auto_replenish_total` counter labeled by trigger. Missing: alerting rules, distributed tracing, Victoria-stack (all tracked). |
| Security review checklist | C | `docs/SECURITY.md` covers key custody, secret handling, fail-closed billing; rate-limiting + idempotency live; no formal audit performed. |
| Frontend portal | B | Sidebar shell, hero-metric dashboard, API keys, Catalog, Activity, login + signup + forgot-password + verify-email + resend-verification. zinc+emerald design system mirroring `livepeer-open-clearinghouse`. No e2e tests (tracked). |
| Frontend admin | B | Sidebar shell, Overview, Users, Pending, Catalog, Audit log, Deposits. zinc+sky design system. No e2e tests (tracked). |
| Reference SDKs | A- | Python, TypeScript, Go, Rust — all four with lint configs (ruff / ESLint+Prettier / golangci-lint / clippy-pedantic) and coverage gates (96%, 100% lines, 90%, 98%). HTTP layer stubbed in tests. Missing: live-shape verification (tracked — the recent orch-count bug + SDK type drift came from this gap). |

## How to use this file

- Update the grade when you ship work that materially changes a domain's
  quality (up or down).
- Don't grade aspirationally. A domain that "should be A" but has no tests
  is a C.
- If you down-grade, write what changed in the "Notes" column.
- This file is a snapshot of *current state*, not a roadmap. Aspirational work
  lives in Beads.
