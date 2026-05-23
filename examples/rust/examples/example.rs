//! End-to-end example: mint a payment, simulate sending to an orch, reconcile usage.
//!
//! ```bash
//! PYMTHOUSE_URL=http://localhost:8000 \
//! PYMTHOUSE_API_KEY=pymth_live_... \
//! cargo run --example example
//! ```

use std::env;

use pymthouse_sdk::{
    Client, ClientOptions, ErrorKind, MintPaymentInput, PymtHouseError, ReportUsageInput,
};

#[tokio::main]
async fn main() -> Result<(), PymtHouseError> {
    let base_url = env::var("PYMTHOUSE_URL").expect("missing PYMTHOUSE_URL");
    let api_key = env::var("PYMTHOUSE_API_KEY").expect("missing PYMTHOUSE_API_KEY");

    let ph = Client::new(ClientOptions::new(base_url, api_key))?;

    // 1. Pick an offering
    let caps = ph.list_capabilities().await?;
    let chat_cap = caps
        .iter()
        .find(|c| c.name == "openai:chat-completions")
        .ok_or_else(|| PymtHouseError::Config("no chat-completions capability".into()))?;
    let offering = chat_cap
        .offerings
        .first()
        .ok_or_else(|| PymtHouseError::Config("no offerings".into()))?;
    println!("using offering: {}", offering.id);

    // 2. Mint with a 1000-token budget; one Idempotency-Key per logical request
    let idem = format!("{:032x}", rand_u128());
    let mint = match ph
        .mint_payment(MintPaymentInput {
            capability: "openai:chat-completions",
            offering: &offering.id,
            work_units: 1000,
            idempotency_key: Some(&idem),
        })
        .await
    {
        Ok(m) => m,
        Err(err) => match err.kind() {
            ErrorKind::InsufficientCredit => {
                eprintln!("need topup: {err}");
                return Ok(());
            }
            ErrorKind::NoRouteAvailable => {
                eprintln!("no orch advertising this offering — try another");
                return Ok(());
            }
            ErrorKind::RateLimited => {
                eprintln!("rate limited; retry in {:?}s", err.retry_after_seconds());
                return Ok(());
            }
            _ => return Err(err),
        },
    };
    println!(
        "minted: work_id={}… ev={}",
        &mint.work_id[..16.min(mint.work_id.len())],
        mint.expected_value_wei
    );
    println!("orch: {}", mint.recipient_eth_address);
    println!(
        "Livepeer-Payment header (truncated): {}…",
        &mint.payment_bytes[..48.min(mint.payment_bytes.len())]
    );

    // 3. Real code POSTs to the orch's URL here. Pretend it consumed 873 tokens.
    let actual_tokens = 873;

    // 4. Reconcile
    let result = ph
        .report_usage(ReportUsageInput {
            payment_id: &mint.payment_id,
            actual_work_units: actual_tokens,
            idempotency_key: Some(&idem),
        })
        .await?;
    println!(
        "refunded {} wei; new balance {} wei",
        result.refunded_wei, result.new_balance_wei
    );
    Ok(())
}

// Avoids pulling in the `rand` crate just for an idempotency key.
fn rand_u128() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    nanos.wrapping_mul(0x9E37_79B9_7F4A_7C15)
}
