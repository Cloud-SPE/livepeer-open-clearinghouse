# Livepeer Open Clearinghouse — example programs

Small reference programs that consume the SDKs in [`../sdks/<lang>/`](../sdks/).
Each scenario is implemented in all four languages.

| Scenario          | What it shows                                                                                   |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `one-shot-job/`   | Submit a single job via the handoff-mode SDK and read the final settlement.                     |
| `streaming-ws/`   | Open `paid-session/v1` with an optional events WebSocket, observe a refill callback, and close. |
| `streaming-http/` | Open an extensible `paid-session/v1`, pass a normative low balance to the runner, and close.    |

Each `<scenario>/` directory has its own per-language manifest
(`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`) wired to
the local sibling SDK via the repo's workspaces (pnpm-workspace.yaml,
`[tool.uv.workspace]`, the Cargo `[workspace]`, and `go.work`). Editing
a SDK and re-running an example here works without a publish step.

## Run an example

All examples expect `OPEN_CLEARINGHOUSE_URL` and `OPEN_CLEARINGHOUSE_API_KEY`.
`one-shot-job` also reads an optional `OPEN_CLEARINGHOUSE_OFFERING` (default
`gpt-oss-20b`); list live offerings with `GET /v1/capabilities`. The
streaming scenarios target illustrative session capabilities, so they need a
broker that advertises them.

Every paid call needs a caller key. Each example generates an ephemeral
secp256k1 key and passes the SDK its compressed public key plus a callback
that signs the `Livepeer-Caller-Proof` (EIP-191 over
`keccak256("livepeer-invocation-proof/v1\x00" || authorization)`, returned
as base64 `R || S || V`). The SDK never holds the private key; production
callers keep their own.

```bash
# TypeScript (from repo root)
pnpm install
pnpm --filter @livepeer/example-one-shot-job start

# Python (from repo root)
uv sync
uv run --package loc-example-one-shot-job python examples/python/one-shot-job/main.py

# Rust (from repo root)
cargo run -p one-shot-job-example

# Go (from repo root)
go run ./examples/go/one-shot-job
```

Substitute the package/crate/module name for the other scenarios
(`streaming-ws-example`, `streaming-http-example`, etc.).
