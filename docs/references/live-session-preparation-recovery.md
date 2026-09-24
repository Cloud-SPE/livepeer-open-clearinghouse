# Live session preparation recovery — September 23, 2026

## Review result

The transcode team's `docs/proposals/loc-live-recovery/TEAM-HANDOFF.md` identified
an unhandled `SelectMany NOT_FOUND` and a committed preparation claim that
remained in flight until its 300-second timeout. The reviewed LOC change adds
consistent selection error mapping and a configurable 45-second RPC deadline,
then releases known failed preparations for immediate identical retry.

Changes beyond the proposal are necessary for reliable recovery:

- Apply the deadline and error mapping to both `Select` and `SelectMany`, covering
  preparation with and without an explicit route binding.
- Do not cache empty `SelectMany` results; the old cache would preserve a
  recovered registry's negative result for another 60 seconds.
- Retain the original lease timestamp on release and advance it strictly on
  reclaim. Expiring by overwriting the timestamp with `now` can reuse a fence
  on an immediate retry. Match the broker request ID as well as account, key,
  operation, status, expiry, and absence of a payment.
- Capture scalar account/key IDs before rollback, which can expire ORM objects.

The [reliability contract](../RELIABILITY.md#pre-payment-session-preparation-recovery)
describes the state transition. Schema revision stays at `0028`; no data reset
or migration accompanies this change. Paid-open and uncertain-funding recovery
retain their existing behavior.

## Registry availability findings

The handoff reports a single configured production RPC host, `arb1.xode.app`.
The local `.env` also configures only that host. Both Compose manifests already
forward the ordered `CHAIN_RPC_URLS` list to both daemons. The accepted
[production topology](../design-docs/001-production-topology.md) requires at least
two ordered independent Arbitrum endpoints. The repository `.env.example`
already contains two public examples; production endpoints remain an operator
choice. Endpoint URLs can contain credentials and must not appear in evidence.

Read-only probes from the development host on September 23 at approximately
21:46 UTC returned Arbitrum One chain ID `0xa4b1` from `arb1.xode.app`,
`arb1.arbitrum.io`, and `arbitrum.publicnode.com`. Chain-ID latency was
0.30–0.42 seconds and block-number latency 0.05–0.13 seconds; observed heads
were within one block. This demonstrates current reachability from this host,
not production health during the 13:44–13:49 incident, contract-read support,
or healthy cold registry selection latency.

A separate source-level problem exists in the sibling Modules checkout:
`chain-commons/providers/rpc/multi/multi.go`, `callWithRetry`, creates a
per-attempt timeout context but discards it; the RPC callback captures the
outer context. A hanging primary can therefore delay fallback beyond the
configured per-attempt budget. Correlate the deployed image revision before
attributing the incident to this code. Bead `loc-5fx` records the upstream
context-propagation fix and required hanging-primary regression. Merely adding
a fallback cannot prove timely recovery; use a healthy primary and test actual
failover with the deployed daemon.

## Deployment and verification procedure

This review changes the LOC checkout only. The handoff explicitly reserves
production changes for coordinated deployment. Deployment and complete media
verification are recorded in `loc-0do`; the LOC code review is `loc-67t`.

1. Select two independent operator-approved Arbitrum endpoints. From the actual
   daemon network, check chain ID 42161, current block head, and the Controller,
   ServiceRegistry, and BondingManager reads used by the registry. Put the
   healthy endpoint first in `CHAIN_RPC_URLS` and test fallback against a failed
   primary in staging. Check the Modules revision for the timeout defect above.
2. Build and publish the reviewed LOC revision as an immutable image. Set
   `REGISTRY_SELECTION_TIMEOUT_SECONDS=45` in the gateway environment. Measure
   cold and warm selection for `video:transcode.live / gateway-ingest`; adjust
   the deadline from evidence while keeping it below the caller HTTP timeout
   and preparation claim timeout. The reported caller budget was about 30s,
   so it must be raised above 45 seconds (with response-processing headroom)
   to use the new default. Include every ingress/proxy timeout in that review.
   The initial patch used 10 seconds; on September 24 the operator selected
   45 seconds to allow more time for cold, geographically distributed lookups.
   This is a configurable operational budget, not a measured latency guarantee.
3. Apply the RPC configuration to the daemons and replace the gateway using the
   deployment's existing Compose project and env file. Preserve database,
   payer wallet, payer DB, volumes, secrets, and existing session/accounting
   rows. Avoid a mixed old/new gateway rollout: only the new version recognizes
   an early `expired` preparation whose lease timestamp is still in the future.
4. Verify registry health and signed route discovery, then prepare and open one
   live session. Exercise a registry timeout in staging: the HTTP response must
   be structured 503, an immediate same-key retry must reach selection, changed
   content must remain 409, and a successful replay must return the same token
   and session ID without another selection. A genuine missing bound candidate
   remains `route_binding_mismatch`; do not change its binding under the same key.
5. Coordinate the transcode runner's termination-reason compatibility release
   before exercising create → RTMP → HLS → close → signed LOC settlement.
   Verify balances, holds, wholesale funding, and final usage. A stuck old
   session is not authority to delete accounting or payment records.

Rollback replaces the gateway with the prior immutable image and retains all
financial state. An already released preparation may wait until its retained
lease expiry under old code; do not clear the row to avoid that delay. RPC
availability and runner compatibility have separate deployment ownership.

## Validation of the reviewed change

- `make check`: Ruff lint/format, layer checks, and strict mypy passed (112
  source files).
- `uv run pytest -q --tb=short --no-showlocals -r fE`: **467 passed, 110 skipped**.
  The skips are existing legacy ticket fixtures; this change adds no skips.
- `make test-conformance`: **3 passed**, with two existing WebSockets
  deprecation warnings. These fixtures are not evidence of the real media
  lifecycle or production deployment.
- A disposable PostgreSQL 16 instance passed 13 recovery, HTTP, and protected
  state cases drawn from `test_create_idempotency.py`. An eight-way concurrent
  immediate retry had exactly one winner, retained the original request ID,
  and resisted a delayed release from the original attempt. The instance was
  removed after validation; no existing database was used.

Regressions cover NOT_FOUND, transport/deadline errors, sanitized responses,
bound and unbound preparation, repeated failure then success, exact successful
replay, changed-content rejection, no negative caching, concurrent timeout and
early-release reclaim, clock regression, and protection of paid/terminal
claims. The SDK suites and full production RTMP/HLS lifecycle were not run for
this backend change. Healthy production cold-selection latency, RPC failover,
and runner-compatible settlement remain deployment evidence to collect.
