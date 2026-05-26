# TypeScript SDK conformance runner

Placeholder — implements the same scenarios as
`conformance/runners/python/` but drives the TypeScript SDK from
`examples/typescript/`.

## Implementation steps

1. **Spawn the mocks** via:
   ```bash
   python -m conformance.mock_loc --scenario <path>.json
   python -m conformance.mock_broker --scenario <path>.json
   ```
   Each prints a JSON line `{"port": N}` on stdout once bound; the
   runner reads that line to learn the chosen port.

2. **Substitute `{BROKER_URL}`** in the scenario file with
   `http://127.0.0.1:<broker-port>` before passing it to mock_loc,
   matching the Python runner's behavior in `conftest.py`.

3. **Instantiate the TS SDK** pointing at the LOC mock URL with
   API key `pymth_live_conformance`:
   ```ts
   import { OpenClearinghouseClient } from "@livepeer/open-clearinghouse-sdk";
   const client = new OpenClearinghouseClient({
     baseUrl: `http://127.0.0.1:${locPort}`,
     apiKey: "pymth_live_conformance",
   });
   ```

4. **Run the scenario steps** (e.g. `submitJob`, `openSession`).

5. **Inspect the call log** via `GET http://127.0.0.1:<port>/_test/inspect`
   and assert the expected wire shape:
   - `livepeer-open-clearinghouse-sdk` header on every LOC call
   - settle retries on 5xx
   - telemetry batch delivered with `request.*` events
   - no body content leaked into telemetry payloads

Use the Python runner (`runners/python/test_*.py`) as the assertion-
shape reference — each TS test should mirror one Python test.
