//! Tests for the Rust SDK telemetry emitter.

use std::sync::Arc;
use std::time::Duration;

use livepeer_open_clearinghouse_sdk::{EmitOptions, EmitterConfig, TelemetryEmitter};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn emitter(server: &MockServer, cfg: EmitterConfig) -> Arc<TelemetryEmitter> {
    let http = reqwest::Client::new();
    TelemetryEmitter::new(
        http,
        &server.uri(),
        "pymth_live_test".into(),
        "rust/0.0.1/dev".into(),
        cfg,
    )
}

#[tokio::test]
async fn emit_flushes_critical_immediately() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .expect(1..)
        .mount(&server)
        .await;
    let em = emitter(
        &server,
        EmitterConfig {
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    em.emit("session.refill_denied", EmitOptions::default()).await;
    // Allow the loop to wake.
    tokio::time::sleep(Duration::from_millis(200)).await;
    em.close().await;
    // mock.expect(1..) verifies via Drop.
}

#[tokio::test]
async fn emit_flushes_at_batch_size() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .expect(1..)
        .mount(&server)
        .await;
    let em = emitter(
        &server,
        EmitterConfig {
            batch_size: 3,
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    em.emit("request.mint_started", EmitOptions::default()).await;
    em.emit("request.mint_completed", EmitOptions::default()).await;
    em.emit("request.broker_call_started", EmitOptions::default())
        .await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    em.close().await;
}

#[tokio::test]
async fn gzip_applied_for_large_body() {
    let server = MockServer::start().await;
    // Match a request that carries Content-Encoding: gzip.
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .and(header("Content-Encoding", "gzip"))
        .respond_with(ResponseTemplate::new(202))
        .expect(1..)
        .mount(&server)
        .await;
    let em = emitter(
        &server,
        EmitterConfig {
            flush_interval_ms: 60_000,
            gzip_threshold_bytes: 32,
            ..Default::default()
        },
    );
    let big = "x".repeat(2000);
    em.emit(
        "session.refill_denied", // critical → immediate flush
        EmitOptions {
            payload: Some(serde_json::json!({ "big": big })),
            ..Default::default()
        },
    )
    .await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    em.close().await;
}

#[tokio::test]
async fn close_drains_remaining() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .expect(1..)
        .mount(&server)
        .await;
    let em = emitter(
        &server,
        EmitterConfig {
            batch_size: 999,
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    em.emit("request.mint_started", EmitOptions::default()).await;
    em.close().await;
}
