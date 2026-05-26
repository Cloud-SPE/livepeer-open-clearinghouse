# Go SDK conformance runner

Placeholder — implements the same scenarios as the Python runner but
drives the Go SDK from `sdks/go/`.

## Implementation steps

1. **Spawn the mocks** via `os/exec` running:
   ```
   python -m conformance.mock_loc --scenario <path>.json
   python -m conformance.mock_broker --scenario <path>.json
   ```
   Read the chosen port from stdout (JSON line `{"port": N}`).

2. **Substitute `{BROKER_URL}`** in the scenario file with
   `http://127.0.0.1:<broker-port>` before passing it to mock_loc.

3. **Instantiate the Go SDK** with the LOC mock URL and API key
   `pymth_live_conformance`.

4. **Exercise the scenario** (mint, settle, refill, close, etc.).

5. **Inspect** via `GET /_test/inspect` and assert. Use
   `runners/python/test_*.go` as the assertion-shape reference.

## Suggested layout

```
runners/go/
├── README.md (this file)
├── go.mod
├── harness/        — shared helpers (process spawn, port discovery, inspect parse)
├── case_a_test.go
├── case_d_test.go
└── settle_retry_test.go
```
