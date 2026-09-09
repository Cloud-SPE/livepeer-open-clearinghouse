# 006 — Usage visibility

| | |
|---|---|
| Domain | `usage` |
| Status | shipped |
| Updated | 2026-09-08 |

> This specification describes the current legacy accounting projection. The
> future wholesale-account design keeps the customer concepts of available,
> held, and spent but derives them from LOC's separate customer ledger, not
> from wholesale ticket funding. See
> [`002-fair-wholesale-credit-accounts.md`](../design-docs/002-fair-wholesale-credit-accounts.md).

## What the user sees

A developer's credit has three states, and both consoles name them the same
way:

- **Available** — credit that can fund new work. This is the `credit_balance`
  row; it is already net of anything held.
- **Held** — funds encumbered by jobs and sessions that have not settled yet.
  The sum of `funded_value_wei` over `payment_session` rows in `open` or
  `draining` state.
- **Spent** — what settled work actually billed. The sum of `billed_value_wei`
  over `closed` rows, shown for the current billing period (against the spend
  cap when one is set) and for the last 30 days.

Usage is derived from `payment_session` rows only. Jobs (`paid-job/v1`) and
sessions (`paid-session/v1`) share that table, and each row already carries
capability, offering, API key, protocol, units, funded, billed and timestamps.
There is no separate usage ledger.

## Customer surface (`/v1/accounts/me/usage`)

Accepts either the portal session cookie or an API key.

| Route | Returns |
|---|---|
| `GET /overview` | available, held, spent this period, spent 30 d, open job count, the billing period (start, end, seconds, cap, percent used) and billed-per-day for 30 days |
| `GET /jobs` | newest-first page of jobs with capability, offering, key label, units and work unit, funded, billed, refund, held, state, accounting outcome, timestamps and duration. Filters: `capability`, `offering`, `api_key_id`, `state` (`open` or `closed`), `since`, `until`; `limit` up to 500 and `offset` |
| `GET /summary` | totals for a window (default last 30 days) plus breakdowns by offering, by API key and by day |

The portal renders these as the dashboard figures, a Usage page with the
job table, filters and CSV export, and a spend-per-key column on the API
keys page.

## Operator surface (`/v1/admin`)

Requires an operator bearer token.

| Route | Returns |
|---|---|
| `GET /users/{id}/usage/{overview,jobs,summary}` | the customer views for one user |
| `GET /usage/summary` | fleet totals with the same breakdowns plus by user |
| `GET /usage/jobs` | fleet job page with `user_id` and `user_email` on each row; adds a `user_id` filter |
| `GET /usage/attention` | what needs an operator: stale open jobs, settlement-verification failures, zero-output sessions (including signed `output_failed` diagnoses), and unterminated sessions still holding funds |

Settlement failures are recorded as `server.settlement_verification_failed`
telemetry events when a job settle or session close is refused, so a broker
that signs with an undelegated key shows up on the overview instead of only
in the customer's SDK error.

## Operator recourse

`POST /v1/admin/jobs/{id}/resolve` with `action` of `refund_hold`,
`accept_reported` or `charge_full` (and an optional `note`) closes a job or
session that cannot settle on its own. Each usage row and each attention entry
carries `blocked_reason` when the reconciler has stopped retrying a record
that can never verify against the pinned snapshot; those rows are the ones to
resolve. Every resolution writes a settlement event and an operator audit row.

## Accounting outcome

Every job row carries one of:

- `open` — funds held, work may still be running
- `unresolved` — open for longer than the stale threshold, or blocked on a
  record that cannot verify (`blocked_reason` says why); the SDK never settled
  and reconciliation cannot recover it
- `broker_settled` — closed on a verified broker settlement
- `conservative_full_charge` — closed by the operational deadline without a
  settlement; the full funded value was charged

## Wire format

Every wei amount is an integer string, never a JSON number or exponent
notation. Consumers parse with `BigInt` or an arbitrary-precision integer.
The same serializer now covers balance, ledger, payment and admin views.

## Failure modes

- No jobs in the window: empty lists, zero totals, HTTP 200.
- Unknown user id on an operator route: empty usage, not 404, since the
  views are aggregates.
- A row whose route snapshot lacks a work unit reports `units`.

## Changelog

- 2026-09-08 — operator resolve action and `blocked_reason` added; wei on job and
  session responses became integer strings.
- 2026-09-08 — first shipped version; replaced the never-written
  `usage_record` table (migration 0024) with reads over `payment_session`.
