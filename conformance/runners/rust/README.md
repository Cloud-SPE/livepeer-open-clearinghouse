# Rust SDK conformance runner

Placeholder — implements the same scenarios as the Python runner but
drives the Rust SDK from `examples/rust/`.

## Implementation steps

1. **Spawn the mocks** via `std::process::Command` running:
   ```
   python -m conformance.mock_loc --scenario <path>.json
   python -m conformance.mock_broker --scenario <path>.json
   ```
   Each prints `{"port": N}` on stdout once bound.

2. **Substitute `{BROKER_URL}`** in the scenario JSON with
   `http://127.0.0.1:<broker-port>` before passing it to mock_loc.

3. **Instantiate the Rust SDK** with the LOC mock URL and API key
   `pymth_live_conformance`:
   ```rust
   let client = Client::new(ClientOptions::new(&loc_url, "pymth_live_conformance"))?;
   ```

4. **Drive the scenario** (mint, settle, refill, close).

5. **Inspect** via `reqwest::get(format!("{loc_url}/_test/inspect"))`
   and assert on the call log. The Python runner's tests are the
   reference shape — port each `test_*.py` to its `tests/*.rs`
   counterpart.

## Suggested layout

```
runners/rust/
├── README.md (this file)
├── Cargo.toml
├── src/
│   └── harness.rs   — shared helpers (process spawn, port discovery)
└── tests/
    ├── case_a.rs
    ├── case_d.rs
    └── settle_retry.rs
```
