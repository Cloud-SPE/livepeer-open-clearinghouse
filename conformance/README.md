# LOC Conformance Harness

Cross-language fixture suite that proves an SDK conforms to the LOC
control-plane + broker data-plane contracts.

The two production-facing servers are mocked here — `mock_loc` mirrors
the LOC HTTP surface, `mock_broker` mirrors the broker — so every SDK
runs against the same wire-level fixtures regardless of language.

## Layout

```
conformance/
├── mock_loc/         FastAPI mock of the LOC control plane
├── mock_broker/      FastAPI mock of the broker data plane
├── scenarios/        JSON specs the mocks load (case-a, case-d-bounded, …)
├── runners/
│   ├── python/       Python-SDK conformance driver (pytest)
│   ├── typescript/   TS-SDK driver (vitest) [placeholder]
│   ├── go/           Go-SDK driver (go test) [placeholder]
│   └── rust/         Rust-SDK driver (cargo test) [placeholder]
└── README.md
```

## How a runner works

1. The runner picks a scenario file (e.g. `scenarios/case_d_extensible.json`).
2. It spawns `mock_loc` + `mock_broker` with that scenario loaded.
3. The mocks bind to `127.0.0.1:<random-port>` and expose two surfaces:
   - the LOC / broker API surface itself (the SDK calls these)
   - a `/_test/*` control surface (the runner inspects calls + resets state)
4. The runner instantiates its SDK pointed at the mock LOC URL.
5. It exercises a sequence of SDK calls described by the scenario.
6. It POSTs to `/_test/inspect` on each mock to retrieve the call log
   and asserts the SDK behaved correctly (e.g. emitted the expected
   telemetry events, sent the right headers, retried on 5xx).

The scenario file is language-agnostic — every SDK can run the same
file. SDK-specific test wiring (how the SDK is imported, how the
process is started) lives in the per-language runner.

## Scenario file format

```json
{
  "id": "case_d_extensible_session",
  "description": "session-control-plus-media@v0 with one auto-refill",
  "loc": {
    "responses": {
      "POST /v1/sessions/open": { ... },
      "POST /v1/sessions/{id}/refill": { ... },
      "POST /v1/sessions/{id}/close": { ... }
    }
  },
  "broker": {
    "responses": {
      "POST /v1/cap": { ... },
      "WS /v1/session/stream": { "frames": [...] }
    }
  },
  "expected_calls": {
    "loc": [
      { "method": "POST", "path": "/v1/sessions/open", "headers": { "Livepeer-Open-Clearinghouse-SDK": "*" } },
      { "method": "POST", "path": "/v1/telemetry", "min_count": 1 }
    ]
  }
}
```

## Running

```bash
# From repo root:
uv pip install -e conformance/mock_loc -e conformance/mock_broker
uv run pytest conformance/runners/python/
```

The runner module starts and tears down the mocks per scenario; no
external services are required.

## Contract assertions covered

- SDK identity header (`Livepeer-Open-Clearinghouse-SDK`) on every LOC call
- Telemetry batch shape + privacy invariants (no body content)
- Settle retry on 5xx / 429 / transport errors; fail-fast on 4xx
- Refill wire shape correct per mode (JSON frame for `session-control-plus-media@v0`,
  POST `{control.topup_url}` for `live-session-*`, no refill for `ws-realtime@v0`)
- HTTP trailers parsed for `http-stream@v0`
- Winddown callback fires for (d-bounded) when cap_status reports imminent
- All mandatory `session.*` and `request.*` telemetry events emitted

## Adding a new SDK runner

The non-Python directories under `runners/` contain placeholder READMEs
documenting the per-language steps. The contract is:

1. Spawn the mocks via `python -m conformance.mock_loc --scenario <file>`
   and `python -m conformance.mock_broker --scenario <file>` — they
   print the chosen port on stdout.
2. Run your SDK against `http://127.0.0.1:<loc-port>` (LOC URL) with
   the API key `pymth_live_conformance`.
3. After the SDK exercise, GET `http://127.0.0.1:<loc-port>/_test/inspect`
   to retrieve the recorded LOC-side call log; ditto for the broker.
4. Assert your runner's expectations.

The Python runner is the reference — copy its assertion library shape
when porting.
