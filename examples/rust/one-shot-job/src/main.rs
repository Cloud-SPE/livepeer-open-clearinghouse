//! End-to-end example: submit a job via the handoff-mode SDK.
//!
//! ```bash
//! OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//! OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//! cargo run -p one-shot-job-example
//! ```
//!
//! The SDK handles the handoff dance: opens a job via POST /v1/jobs
//! (mints a payment envelope), calls the broker directly with the
//! envelope as Livepeer-Payment, reads Livepeer-Work-Units from the
//! broker response, and posts settle back to LOC.

use std::env;

use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, ErrorKind, JobBody, OpenClearinghouseError, SubmitJobInput,
};
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = env::var("OPEN_CLEARINGHOUSE_URL")?;
    let api_key = env::var("OPEN_CLEARINGHOUSE_API_KEY")?;

    let client = Client::new(ClientOptions::new(base_url, api_key))?;

    let result = match client
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 200,
            max_total_units: Some(2000),
            body: JobBody::Json(json!({
                "messages": [{"role": "user", "content": "explain handoff mode"}],
                "max_tokens": 500
            })),
            request_id: None,
            spec_version: None,
        })
        .await
    {
        Ok(r) => r,
        Err(OpenClearinghouseError::Api {
            kind,
            code,
            message,
            ..
        }) => {
            let code_s = code.unwrap_or_default();
            match kind {
                ErrorKind::InsufficientCredit => println!("not enough credit"),
                ErrorKind::NoRouteAvailable => {
                    println!("no orch advertising this capability/offering")
                }
                ErrorKind::RateLimited => println!("rate limited"),
                _ => println!("loc error: {code_s} - {message}"),
            }
            return Ok(());
        }
        Err(e) => return Err(e.into()),
    };

    if result.status == 200 {
        println!("==== broker response ====");
        if let Some(b) = &result.body {
            println!("{b}");
        } else {
            println!("{}", result.body_text);
        }
        println!();
        println!("==== final accounting ====");
        println!("actual units consumed: {}", result.actual_units);
        println!("billed:                {} wei", result.billed_value_wei);
        println!("refund:                {} wei", result.refund_wei);
        println!("outcome:               {}", result.outcome);
        if result.cap_status.will_refuse_next_refill {
            let reason = result
                .cap_status
                .winddown_reason
                .as_deref()
                .unwrap_or("unknown");
            println!("⚠️  cap warning: {reason} — another job at this size may be refused");
        }
    } else {
        println!("broker returned {}", result.status);
        println!("{}", result.body_text);
    }
    Ok(())
}
