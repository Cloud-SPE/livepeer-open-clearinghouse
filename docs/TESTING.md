# Test and conformance baseline

The release gate is reproducible from a clean checkout with:

```bash
uv sync --frozen
pnpm install --frozen-lockfile
make test-release
```

`make test-release` runs LOC lint, layering, typing, and all collected backend
tests; the `paid-job/v1` and `paid-session/v1` shared fixture harness; and the
native quality/test suites for the Python, TypeScript, Go, and Rust SDKs.

`tests/integration/` and `tests/e2e/` currently contain no executable cases;
the backend's database and composed handoff tests run hermetically under the
unit marker. Live daemon/broker testing belongs to the nightly conformance gate
tracked by `loc-m7s.10.2`, not to a silently skipped local suite.

There are no LOC-owned fuzz targets in this repository. Protocol parser and
state-machine fuzzing lives with the Go implementations in Livepeer Modules;
the current Modules tree exposes no `Fuzz*` Go targets. If either repository
adds a fuzz target, it must be added to this release command or tracked by a
Bead before a failure can be waived.

The TypeScript, Go, and Rust shared-fixture runners are tracked by
`loc-m7s.10.3`. Their native suites already prove the v2 wire contract and are
mandatory here; the placeholder runner directories are not represented as
passing tests.

The first real-process nightly preflight is available locally when a
`livepeer-network-modules` checkout exists beside this repository:

```bash
make test-live-registry
```

It builds the actual service-registry daemon, serves a freshly signed
coordinator manifest, replaces only the chain's address-to-serviceURI lookup
with Modules' `--chain-seed`, and verifies through the Unix-socket gRPC API that
both paid-v2 protocol axes and the delegated settlement key reach `Select`.
Logs and a machine-readable result are left under
`.artifacts/live-conformance/registry-seed/`.

The complete real-process preflight is:

```bash
make test-live-stack
```

It source-builds and starts the payment daemon in payer and payee modes, the
capability broker, and the service-registry daemon from the sibling Modules
checkout. It also starts an isolated Postgres container, applies every LOC
migration, and runs the LOC gateway with its real daemon clients. Only the
chain lookup and a trivial workload backend are fakes. The command proves LOC
health and performs signed route selection through LOC's production UDS gRPC
client. Every process log, build log, migration log, and a machine-readable
result is retained under `.artifacts/live-conformance/stack/`; processes and
the Postgres container are removed on success or failure.
