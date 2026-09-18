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
    em.emit("session.refill_denied", EmitOptions::default())
        .await;
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
    em.emit("request.mint_started", EmitOptions::default())
        .await;
    em.emit("request.mint_completed", EmitOptions::default())
        .await;
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
    em.emit("request.mint_started", EmitOptions::default())
        .await;
    em.close().await;
}

#[test]
fn correlation_id_passes_uuid_through_lowercased() {
    use livepeer_open_clearinghouse_sdk::telemetry_correlation_id;
    assert_eq!(
        telemetry_correlation_id("8D5C0E8A-2B7F-4C3D-9E1A-0F6B7C8D9E0F"),
        "8d5c0e8a-2b7f-4c3d-9e1a-0f6b7c8d9e0f"
    );
    assert_eq!(
        telemetry_correlation_id("8d5c0e8a2b7f4c3d9e1a0f6b7c8d9e0f"),
        "8d5c0e8a-2b7f-4c3d-9e1a-0f6b7c8d9e0f"
    );
}

#[test]
fn correlation_id_derives_uuid5_for_non_uuid_values() {
    use livepeer_open_clearinghouse_sdk::telemetry_correlation_id;
    // python3 -c "import uuid; print(uuid.uuid5(uuid.NAMESPACE_URL,'loc-test-chat-abc'))"
    assert_eq!(
        telemetry_correlation_id("loc-test-chat-abc"),
        "ab6ae49d-69c6-579d-8f36-8b7eaeed58a6"
    );
    assert_eq!(
        telemetry_correlation_id("loc-test-chat-abc"),
        telemetry_correlation_id("loc-test-chat-abc"),
        "derivation is deterministic"
    );
}

#[tokio::test]
async fn emit_sends_uuid_correlation_ids_on_the_wire() {
    use wiremock::matchers::body_partial_json;
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .and(body_partial_json(serde_json::json!({
            "events": [{ "correlation_id": "ab6ae49d-69c6-579d-8f36-8b7eaeed58a6" }]
        })))
        .respond_with(ResponseTemplate::new(202))
        .expect(1)
        .mount(&server)
        .await;
    let em = emitter(
        &server,
        EmitterConfig {
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    em.emit(
        "session.closed",
        EmitOptions {
            correlation_id: Some("loc-test-chat-abc".into()),
            ..Default::default()
        },
    )
    .await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    em.close().await;
}
