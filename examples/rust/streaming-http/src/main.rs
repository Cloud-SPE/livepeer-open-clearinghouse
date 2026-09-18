//! Extensible paid-session/v1 session with authoritative HTTP top-up.
//!
//! ```bash
//! OPEN_CLEARINGHOUSE_URL=http://localhost:8000 \
//! OPEN_CLEARINGHOUSE_API_KEY=pymth_live_... \
//! cargo run -p streaming-http-example
//! ```
//!
//! The customer's media plane can pass the broker's normative balance
//! object to `runner.on_balance()`.
//! The runner then asks LOC for a refill and POSTs it to the broker's
//! control.topup_url.

use std::env;
use std::sync::Arc;

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use k256::ecdsa::SigningKey;
use livepeer_open_clearinghouse_sdk::{
    CallerProofSigner, Client, ClientOptions, OpenClearinghouseError, OpenSessionInput,
    RefillEvent, SessionBalance, SessionRunner, SessionRunnerOptions, WinddownEvent,
};
use rand_core::OsRng;
use sha3::{Digest, Keccak256};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let base_url = env::var("OPEN_CLEARINGHOUSE_URL")?;
    let api_key = env::var("OPEN_CLEARINGHOUSE_API_KEY")?;
    let (caller_public_key, caller_signer) = caller_proof();
    let client = Client::new(
        ClientOptions::new(base_url, api_key).with_caller_proof(caller_public_key, caller_signer),
    )?;

    let handle = client
        .open_session(OpenSessionInput {
            capability: "livepeer:remote-runner",
            offering: "live-session-remote-runner",
            descriptor_schema: "livepeer.session.remote-runner/v1",
            session_params: serde_json::json!({}),
            estimated_runway_units: 1000,
            max_total_units: 10000,
            request_id: None,
        })
        .await?;
    println!(
        "session opened: {} (protocol={})",
        handle.session_id, handle.protocol
    );

    let mut opts = SessionRunnerOptions::new(client, handle);
    opts.on_refill_succeeded = Some(Arc::new(|e: RefillEvent| {
        Box::pin(async move {
            println!("refill {:?}: +{:?} wei", e.refill_seq, e.funded_value_wei);
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

    // Customer-driven refill. In production this fires when the media
    // plane observes balance-low on the runner channel.
    runner
        .on_balance(SessionBalance {
            status: "low".to_string(),
            claimed_units: 500,
            debited_units: 500,
            unit: "session_second".to_string(),
            runway_units: 100,
            runway_seconds_estimate: Some(100),
            will_refuse_next_refill: false,
        })
        .await;

    let outcome = runner.close(750).await?;
    println!("==== final settlement ====");
    println!("outcome: {}", outcome.outcome);
    println!("billed:  {} wei", outcome.billed_value_wei);
    println!("refund:  {} wei", outcome.refund_wei);
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
