# Reference: service-registry-daemon

A reference card for the `service-registry-daemon` we integrate with.
Source:
`/home/mazup/git-repos/livepeer-cloud-spe/livepeer-network-modules/service-registry-daemon`.

This is a digest for fast lookup. When the daemon's behavior is the
authority, go read the daemon's source.

## What it is

A Go sidecar that decouples Livepeer orchestrator/worker discovery from
`go-livepeer`. Operators publish a **signed JSON manifest** at a
well-known URL (and point to it via an on-chain `serviceURI`); consumers
fetch and verify the manifest, optionally merge an operator-curated static
overlay, and resolve to a `SelectedRoute` for a given capability+offering.

Two modes:
- **resolver** — what Livepeer Open Clearinghouse uses; reads signed manifests, returns
  routes.
- **publisher** — orchestrator-side; builds and signs manifests.

The daemon is **workload-agnostic.** Capability names like
`openai:chat-completions` or `livepeer:transcoder/h264` are opaque strings;
the consumer interprets them.

## Transport

- **gRPC over a Unix domain socket.**
- Default socket: `/var/run/livepeer-service-registry.sock`.
- **No auth on the gRPC surface.** Local caller is trusted; filesystem
  mediates.
- Livepeer Open Clearinghouse mounts the same `livepeer-run` volume as the daemon and runs
  as uid/gid `65532:65532`.

## RPCs Livepeer Open Clearinghouse calls (resolver mode)

### `Select(capability, offering, tier?, min_weight?) → SelectedRoute`

The load-bearing call. Returns one explicit route for a capability+offering
combination, ranked by weight.

**Response shape** (the part Livepeer Open Clearinghouse uses):

```
SelectedRoute {
    worker_url: string                         # → payment-daemon's ticket_params_base_url
    eth_address: string                        # → payment-daemon's recipient
    capability: string
    offering: string
    price_per_work_unit_wei: string            # → multiply by work_units for funding
    work_unit: string
    quote_id: string                           # → AcceptedPrice.quote_ref.quote_id
    quote_version: uint64
    constraint_fingerprint: bytes              # → quote_ref.constraint_fingerprint
    route_fingerprint: bytes                   # → quote_ref.route_fingerprint
    units_per_price: uint64
    extra: bytes                               # capability-specific opaque data
    constraints: bytes                         # opaque
}
```

Everything `payment-daemon.CreatePayment` needs is here — Livepeer Open Clearinghouse passes
the relevant fields through unchanged.

### `SelectMany(capability, offering, …) → []SelectedRoute`

Same filter as `Select` but returns all payment-ready routes for failover.

### `ResolveByAddress(eth_address, allow_legacy_fallback, allow_unsigned, force_refresh) → ResolveResult`

Returns the full parsed manifest + overlay-merged nodes for a single
orchestrator address. Useful for "show me everything orch X offers."

### `ListKnown() → [KnownEntry]`

Returns all cached addresses with their cache freshness. Useful for the
admin surface and for warming the cache.

### `Refresh(eth_address | "*", force) → ()`

Forces re-fetch. Useful when an operator just published a new manifest.

### `Health() → HealthResult`

Returns daemon liveness: chain RPC ok, manifest fetcher ok, cache size,
last successful chain probe.

## Data model

**Manifest** (signed JSON published by operator):

```
schema_version: "3.0.1"
eth_address: string
issued_at: RFC3339
nodes: [Node]
signature: { alg: "eth-personal-sign", value: 0x..., signed_canonical_bytes_sha256: 0x... }
```

**Node:**

```
id: string
url: string
worker_eth_address?: string
capabilities: [Capability]
extra?: json.RawMessage
```

**Capability:**

```
name: string                # e.g. "openai:chat-completions"
work_unit?: string          # e.g. "token", "frame"
offerings: [Offering]
extra?: json.RawMessage
```

**Offering:**

```
id: string                  # e.g. "gpt-oss-20b", "h264-1080p"
price_per_work_unit_wei?: string
constraints?: json.RawMessage
```

**ResolvedNode** (enriched by the resolver with trust/policy):

```
(inherits Node fields)
source: SourceManifest | SourceLegacy | SourceStaticOverlay | SourceCSVFallback
signature_status: SigVerified | SigUnsigned | SigLegacy | SigInvalid
operator_addr: EthAddress
enabled: bool (overlay)
tier_allowed?: [string]
weight: int (default 100)
```

**ResolveResult:**

```
eth_address: EthAddress
resolved_uri: string
mode: well-known | csv | legacy | static-overlay
nodes: [ResolvedNode]
freshness_status: fresh | stale_recoverable | stale_failing
cached_at: time.Time
fetched_at: time.Time
manifest?: Manifest
schema_version: string
```

## Config (resolver mode)

| Flag | Default | What |
|---|---|---|
| `--mode=resolver` | — | daemon mode |
| `--socket` | `/var/run/livepeer-service-registry.sock` | UDS path |
| `--chain-rpc` | — | Ethereum JSON-RPC; required for chain discovery |
| `--chain-id` | `42161` | sanity-check |
| `--service-registry-address` | (resolved via Controller) | `ServiceRegistry` contract |
| `--store-path` | `/var/lib/livepeer/registry-cache.db` | BoltDB cache |
| `--static-overlay` | optional | operator-curated nodes.yaml (SIGHUP hot-reload) |
| `--discovery` | `chain` | `chain` walks BondingManager; `overlay-only` skips chain |
| `--cache-manifest-ttl` | `10m` | freshness TTL |
| `--manifest-fetch-timeout` | `5s` | HTTP timeout |
| `--manifest-max-bytes` | `4Mi` | DoS cap |
| `--max-stale` | `1h` | last-good fallback max age |
| `--reject-unsigned` | `true` | reject unsigned CSV/overlay entries |
| `--metrics-listen` | optional | Prometheus listener |

## Deployment

- Image: `tztcloud/livepeer-service-registry-daemon:vX.Y.Z`.
- Runs as `65532:65532`, distroless static base.
- Mounts the `livepeer-run` volume for the socket.
- Persists cache to a named volume (`registry-cache` in the reference
  compose) so restarts are warm.
- An optional `nodes.yaml` overlay file mounted read-only.

## Gotchas

- **`Select()` per call for MVP, no caching in Livepeer Open Clearinghouse.** The daemon
  caches manifests; we don't cache routes on top. See
  `tech-debt-tracker.md`.
- **The `quote_ref` triplet from `Select` flows straight into
  `CreatePayment`.** Don't synthesize it; pass it through.
- **`signature_status: SigVerified` is what we want.** A `SigInvalid`
  manifest is rejected before it's cached; we shouldn't see it. We may
  see `SigUnsigned` from overlay/CSV — operator policy decides whether to
  accept it.
- **`freshness_status: stale_failing` means the daemon couldn't refresh
  the manifest** but is returning the last-good entry. We treat this as
  acceptable for `Select` but should surface a warning in the admin UI.
- **No streaming RPCs.** No way to subscribe to "manifest changed";
  call `Refresh` if you need a re-pull.
- **`examples/minimal-e2e/main.go`** is a working Go integration example
  worth reading once.

## Key source paths

- `internal/runtime/grpc/server.go` — gRPC handler surface
- `internal/types/manifest.go` — manifest / node / capability / offering
- `internal/types/resolved.go` — `ResolveResult`, `ResolvedNode`
- `internal/config/daemon.go` — config + defaults
- `examples/minimal-e2e/main.go` — end-to-end integration sample
- `registry.example.yaml` — static overlay format
- `proto/livepeer/registry/v1/` — proto definitions (in
  `livepeer-network-protocol`)
