//! End-to-end example: submit a job via the handoff-mode SDK.
//!
//! ```bash
//! OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//! OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//! OPEN_CLEARINGHOUSE_OFFERING=gpt-oss-20b \
//! OPEN_CLEARINGHOUSE_MODEL=gpt-oss-20b \
//! cargo run -p one-shot-job-example
//! ```
//!
//! `OPEN_CLEARINGHOUSE_OFFERING` is optional and defaults to `gpt-oss-20b`.
//! `OPEN_CLEARINGHOUSE_MODEL` is the OpenAI model name the offering advertises
//! (its `extra.openai.model`); it defaults to the offering id.
//!
//! The SDK handles the handoff dance: opens a job via POST /v1/jobs
//! for a route-locked spend authorization, calls the broker directly with the
//! authorization and caller proof, reads Livepeer-Work-Units from the
//! broker response, and posts settle back to LOC.

use std::env;
use std::sync::Arc;

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use k256::ecdsa::SigningKey;
use livepeer_open_clearinghouse_sdk::{
    CallerProofSigner, Client, ClientOptions, ErrorKind, JobBody, OpenClearinghouseError,
    SubmitJobInput,
};
use rand_core::OsRng;
use serde_json::json;
use sha3::{Digest, Keccak256};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = env::var("OPEN_CLEARINGHOUSE_URL")?;
    let api_key = env::var("OPEN_CLEARINGHOUSE_API_KEY")?;
    let offering =
        env::var("OPEN_CLEARINGHOUSE_OFFERING").unwrap_or_else(|_| "gpt-oss-20b".to_owned());
    let model = env::var("OPEN_CLEARINGHOUSE_MODEL").unwrap_or_else(|_| offering.clone());

    let (caller_public_key, caller_signer) = caller_proof();
    let client = Client::new(
        ClientOptions::new(base_url, api_key).with_caller_proof(caller_public_key, caller_signer),
    )?;

    let result = match client
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: &offering,
            estimated_units: 200,
            max_total_units: Some(2000),
            body: JobBody::Json(json!({
                "model": model,
                "messages": [{"role": "user", "content": "explain handoff mode"}],
                "max_tokens": 500
            })),
            request_id: None,
            transport: Some("unary"),
            content_type: None,
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

/// Generate a caller key and a `Livepeer-Caller-Proof` signer for it.
///
/// The key is ephemeral (fresh per process run) to keep the example
/// self-contained. Production callers keep and protect their own
/// secp256k1 key; the SDK never takes custody of it.
///
/// Scheme (Modules `headers/livepeer-headers.md`):
/// `digest = keccak256("livepeer-invocation-proof/v1\0" || authorization)`,
/// signed with EIP-191 personal-sign over the 32-byte digest, encoded as
/// base64(R || S || V) with V = 27 + recovery id. The public key is the
/// compressed SEC1 point as lowercase hex without a `0x` prefix.
fn caller_proof() -> (String, CallerProofSigner) {
    let key = SigningKey::random(&mut OsRng);
    let public_key = key
        .verifying_key()
        .to_encoded_point(true)
        .as_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let signer: CallerProofSigner = Arc::new(move |authorization: &[u8]| {
        let digest = Keccak256::new()
            .chain_update(b"livepeer-invocation-proof/v1\x00")
            .chain_update(authorization)
            .finalize();
        let message_hash = Keccak256::new()
            .chain_update(b"\x19Ethereum Signed Message:\n32")
            .chain_update(digest)
            .finalize();
        let (signature, recovery_id) = key
            .sign_prehash_recoverable(&message_hash)
            .map_err(|e| OpenClearinghouseError::Config(format!("caller proof: {e}")))?;
        let mut proof = signature.to_bytes().to_vec();
        proof.push(27 + recovery_id.to_byte());
        Ok(STANDARD.encode(proof))
    });
    (public_key, signer)
}
