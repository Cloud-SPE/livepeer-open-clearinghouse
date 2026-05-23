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

```rust
use livepeer_open_clearinghouse_sdk::{Client, ClientOptions, ErrorKind, MintPaymentInput, ReportUsageInput};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ph = Client::new(ClientOptions::new(
        "https://open-clearinghouse.example.com",
        std::env::var("OPEN_CLEARINGHOUSE_API_KEY")?,
    ))?;

    let idem = "<your-uuid>";
    let mint = match ph
        .mint_payment(MintPaymentInput {
            capability: "openai:chat-completions",
            offering: "vllm-qwen3.6-27b-default",
            work_units: 1000,
            idempotency_key: Some(idem),
        })
        .await
    {
        Ok(m) => m,
        Err(e) if e.kind() == ErrorKind::InsufficientCredit => {
            eprintln!("need topup");
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };

    // ... POST to mint.recipient_eth_address's orch with header
    //     Livepeer-Payment: mint.payment_bytes ...

    ph.report_usage(ReportUsageInput {
        payment_id: &mint.payment_id,
        actual_work_units: 873,
        idempotency_key: Some(idem),
    })
    .await?;
    Ok(())
}
```

Method surface:

| | |
|---|---|
| `list_capabilities()` | discovery |
| `list_orchestrators(capability)` | discovery |
| `mint_payment(MintPaymentInput)` | the load-bearing call |
| `report_usage(ReportUsageInput)` | reconcile over-committed budget |

`OpenClearinghouseError` is a `thiserror` enum with `Transport`, `Api`, and
`Config` variants. Call `.kind()` for the high-level `ErrorKind`
(`InsufficientCredit`, `SpendCapExceeded`, `AccountNotApproved`,
`EmailNotVerified`, `NoRouteAvailable`, `RateLimited`,
`DuplicateRequest`, `DaemonUnavailable`, `Other`). For rate-limit
back-off, `err.retry_after_seconds()` returns the parsed `Retry-After`
header value when present.
