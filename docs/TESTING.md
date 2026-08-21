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
