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
    │   ├── typescript/   reserved for the release-gated runner
    │   ├── go/           reserved for the release-gated runner
    │   └── rust/         reserved for the release-gated runner
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
  "description": "paid-session/v1 extensible session with one refill",
  "loc": {
    "responses": {
      "POST /v1/sessions": { ... },
      "POST /v1/sessions/{id}/refill": { ... },
      "POST /v1/sessions/{id}/close": { ... }
    }
  },
  "broker": {
    "responses": {
      "POST /v1/session": { ... },
      "POST /v1/session/{session_id}/topup": { ... },
      "POST /v1/session/{session_id}/end": { ... }
    }
  },
  "expected_calls": {
    "loc": [
      { "method": "POST", "path": "/v1/sessions", "headers": { "Livepeer-Open-Clearinghouse-SDK": "*" } },
      { "method": "POST", "path": "/v1/telemetry", "min_count": 1 }
    ]
  }
}
```

## Running

```bash
# From repo root:
make test-conformance
```

The runner module starts and tears down the mocks per scenario; no
external services are required.

## Contract assertions covered

- SDK identity header (`Livepeer-Open-Clearinghouse-SDK`) on every LOC call
- Telemetry batch shape + privacy invariants (no body content)
- Settle retry on 5xx / 429 / transport errors; fail-fast on 4xx
- `paid-job/v1` protocol, transport, request identity, work-unit, job-id, and
  signed-settlement wire fields
- `paid-session/v1` broker open, status, top-up and end through the SDK's
  `SessionRunner`, including signed close forwarding to LOC
- Winddown callback fires for (d-bounded) when cap_status reports imminent
- All mandatory `session.*` and `request.*` telemetry events emitted

## Adding a new SDK runner

The non-Python directories are reserved for `loc-m7s.10.3`, which gates each
published SDK on the shared fixtures. Until then, their native suites exercise
the same v2 wire behavior and `make test-sdks` is the release baseline. The
future runner contract is:

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
