# Production capability lookup timeouts — 2026-09-24

Read-only investigation on `infra1.cloudspe.com`, approximately 16:28–16:35 UTC.
Investigation: `loc-uvg`; remediation: `loc-dot`.

## Finding

LOC builds its capability catalog by serially resolving every address returned
by the registry's `ListKnown`, then selecting each discovered offering. Slow,
unreachable manifest endpoints therefore delay the entire catalog, even when
the requested ABR and live routes are available.

The deployed implementation was inspected inside the LOC container. It matches
the aggregation in `providers/registry_daemon/client.py`: `_resolve` and
`_list_known_addresses` have no RPC deadline. The configured 45-second selection
timeout applies to selection RPCs, not these resolution calls. Resolution errors
are skipped with debug logging; production logging is set to info.

Production already has `REGISTRY_CACHE_TTL_SECONDS=300`. The cache only fills
after aggregation completes, drops expired entries, and does not coalesce
concurrent misses. A longer TTL cannot fix cold-start aggregation and only
reduces the frequency of expensive refreshes after a successful fill.

## Evidence

- Video gateway logged repeated `/v1/capabilities` client timeouts at
  16:28:17 through 16:33:17 UTC.
- `/api/admin/registry/candidates` returned 502 at 16:30:11 and 16:30:32,
  after 30,006 and 30,001 milliseconds respectively.
- Registry `ResolveByAddress` logged `Unavailable / manifest_unavailable`;
  observed slow calls took approximately 15–30 seconds each. Manifest refresh
  errors included nonexistent DNS names, connection refusal, unreachable
  hosts, TLS verification failures and upstream timeouts.
- A bounded, read-only `ListKnown` probe returned 52 entries in 22 ms:
  50 `RESOLVE_MODE_LEGACY`, two `RESOLVE_MODE_WELL_KNOWN`. All entries reported
  unspecified freshness, so this field alone cannot identify usable entries.
- Direct selection of `video:transcode.abr / abr-default` succeeded in 935 ms;
  `video:transcode.live / gateway-ingest` succeeded in 10 ms. These probes
  establish route selection availability, not payment admission or execution.
- LOC ran image `tztcloud/livepeer-open-clearinghouse-gateway:v2.0.0`, started
  at 16:26:51 UTC. A separate capabilities HTTP 404 at 16:26:47 preceded this
  start; its precise cause was not established. It does not explain the
  sustained timeout pattern afterward.
- Registry RPC logs reported version `v2.0.0-9ab3a002dfe9`.

## Remediation direction

Bound catalog refresh work with per-RPC and overall deadlines and controlled
concurrency; avoid synchronously crawling irrelevant or unavailable legacy
endpoints to list usable capabilities. Use verified registry metadata where
the daemon contract supports it. Coalesce concurrent refreshes and consider a
previous catalog snapshot for discovery display during a bounded refresh.
Distinguish discovery unavailability from an authoritative empty catalog.

Preserve signature checks and authoritative paid-route validation at issuance.
Do not use cached catalog availability as payment authorization. Raising the
selection timeout does not address the unbounded resolution path.

No production configuration, funds, jobs or containers were changed.
