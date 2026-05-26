//! Streaming session with WS topup (session-control-plus-media@v0).
//!
//! ```bash
//! OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//! OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//! cargo run -p streaming-ws-example
//! ```
//!
//! SessionRunner connects to the broker over a control WebSocket. When
//! the broker pushes a Livepeer-Balance-Low frame, the runner asks LOC
//! for a refill and delivers it back as a session.topup frame — the
//! on_refill_succeeded callback fires on each successful top-up.

use std::env;
use std::sync::Arc;
use std::time::Duration;

use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, OpenClearinghouseError, OpenSessionInput, RefillEvent, SessionRunner,
    SessionRunnerOptions, WinddownEvent,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = env::var("OPEN_CLEARINGHOUSE_URL")?;
    let api_key = env::var("OPEN_CLEARINGHOUSE_API_KEY")?;
    let client = Client::new(ClientOptions::new(base_url, api_key))?;

    let handle = client
        .open_session(OpenSessionInput {
            capability: "livepeer:live-video-control",
            offering: "session-control-plus-media",
            estimated_runway_units: 1000,
            max_total_units: 10000,
        })
        .await?;
    println!("session opened: {} (mode={})", handle.session_id, handle.mode);

    let mut opts = SessionRunnerOptions::new(client, handle);
    opts.on_refill_succeeded = Some(Arc::new(|e: RefillEvent| {
        Box::pin(async move {
            println!(
                "refill {:?}: +{:?} wei",
                e.refill_seq, e.funded_value_wei
            );
        })
    }));
    opts.on_refill_refused = Some(Arc::new(|e: RefillEvent| {
        Box::pin(async move {
            let code = e
                .error
                .as_ref()
                .map(|err| format!("{err}"))
                .unwrap_or_else(|| "unknown".to_string());
            println!("refill refused: {code}");
        })
    }));
    opts.on_winddown_warning = Some(Arc::new(|w: WinddownEvent| {
        Box::pin(async move {
            println!("winddown: {}", w.reason);
        })
    }));

    let runner = match SessionRunner::start(opts).await {
        Ok(r) => r,
        Err(OpenClearinghouseError::Api { code, message, .. }) => {
            println!("loc error: {} - {message}", code.unwrap_or_default());
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };

    // Hold the session briefly so the broker has a chance to push at
    // least one Livepeer-Balance-Low frame. Production code would drive
    // its own media plane on top of this WS rather than sleeping.
    tokio::time::sleep(Duration::from_secs(3)).await;

    let outcome = runner.close(750).await?;
    println!("==== final settlement ====");
    println!("outcome: {}", outcome.outcome);
    println!("billed:  {} wei", outcome.billed_value_wei);
    println!("refund:  {} wei", outcome.refund_wei);
    Ok(())
}
