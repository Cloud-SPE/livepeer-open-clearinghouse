# livepeer-open-clearinghouse-sdk (Rust)

Reference Rust SDK for the Livepeer Open Clearinghouse gateway. Built on `reqwest`
(rustls), `serde`, `thiserror`, `tokio`. No `unsafe`.

## Setup

Rust 1.75+ required.

```bash
cargo build
```

## Run the tests

```bash
cargo test
```

Uses `wiremock` to stub the gateway's HTTP surface; no live Livepeer Open Clearinghouse
needed.

## Coverage

```bash
cargo install cargo-llvm-cov     # one-time
cargo llvm-cov --summary-only    # text
cargo llvm-cov --html            # html in target/llvm-cov/html
```

## Lint + format

```bash
cargo clippy --all-targets -- -D warnings    # lint
cargo fmt --all -- --check                   # fmt check
cargo fmt --all                              # auto-format
```

Clippy presets (in `Cargo.toml` `[lints.clippy]`): `pedantic` + `nursery`
at warn, with a small set of explicit allows for SDK-shape reasons.
`unsafe_code = "forbid"`.

## Run the example against a live stack

```bash
OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
OPEN_CLEARINGHOUSE_API_KEY=pymth_live_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx \
cargo run --example example
```

## Use it from your app

Livepeer Open Clearinghouse runs in **handoff mode**: LOC mints the
payment envelope; the SDK calls the broker directly with that
envelope; LOC settles based on the broker's reported work units.

```rust
use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, ErrorKind, JobBody, SubmitJobInput,
};
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ph = Client::new(ClientOptions::new(
        "https://open-clearinghouse.example.com",
        std::env::var("OPEN_CLEARINGHOUSE_API_KEY")?,
    ))?;

    let result = match ph
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "vllm-qwen3.6-27b-default",
            estimated_units: 200,
            body: JobBody::Json(json!({
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 50,
            })),
            max_total_units: Some(2000),
            request_id: None,
            transport: Some("unary"),
            content_type: None,
        })
        .await
    {
        Ok(r) => r,
        Err(e) if e.kind() == ErrorKind::InsufficientCredit => {
            eprintln!("need topup");
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };
    println!(
        "billed {} wei for {} units, outcome={}",
        result.billed_value_wei, result.actual_units, result.outcome
    );
    Ok(())
}
```

Long-running session shape:

```rust
use livepeer_open_clearinghouse_sdk::{OpenSessionInput, CloseSessionInput};

let handle = ph.open_session(OpenSessionInput {
    capability: "cap.live",
    offering: "off.live",
    estimated_runway_units: 1000,
    max_total_units: 10_000,
}).await?;
// ... stream work against handle.broker_url, refill via SessionRunner ...
ph.close_session(CloseSessionInput {
    session_id: &handle.session_id,
    actual_units: 4250,
}).await?;
```

Method surface:

|                                    |                                                     |
| ---------------------------------- | --------------------------------------------------- |
| `list_capabilities()`              | discovery                                           |
| `list_orchestrators(capability)`   | discovery                                           |
| `submit_job(SubmitJobInput)`       | one-shot job (cases a/b/c)                          |
| `open_session(OpenSessionInput)`   | open long-running session (case d)                  |
| `refill_session(...)`              | top up an open session                              |
| `close_session(CloseSessionInput)` | settle + close a session                            |
| `telemetry()`                      | direct access to the (mandatory) `TelemetryEmitter` |

The `Livepeer-Open-Clearinghouse-SDK` identity header is sent on
every call, and telemetry events (`request.mint_started`,
`request.settle_completed`, `session.opened`, …) fire fire-and-forget
through `/v1/telemetry`. There is no telemetry opt-out.

`OpenClearinghouseError` is a `thiserror` enum with `Transport`, `Api`,
and `Config` variants. Call `.kind()` for the high-level `ErrorKind`
(`InsufficientCredit`, `SpendCapExceeded`, `AccountNotApproved`,
`EmailNotVerified`, `NoRouteAvailable`, `RateLimited`,
`DuplicateRequest`, `DaemonUnavailable`, `Other`). For rate-limit
back-off, `err.retry_after_seconds()` returns the parsed `Retry-After`
header value when present.
