use std::sync::Arc;

use livepeer_open_clearinghouse_sdk::{EmitOptions, EmitterConfig, TelemetryEmitter};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn telemetry_emit_after_close_is_silent() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .mount(&server)
        .await;
    let emitter: Arc<TelemetryEmitter> = TelemetryEmitter::new(
        reqwest::Client::new(),
        &server.uri(),
        "pymth_live_test".into(),
        "rust/0.0.1/dev".into(),
        EmitterConfig {
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    emitter.close().await;
    emitter.emit("post.close", EmitOptions::default()).await;
    assert_eq!(emitter.buffer_size().await, 0);
    assert_eq!(emitter.dropped().await, 0);
}

#[tokio::test]
async fn telemetry_buffer_overflow_drops_oldest() {
    let server = MockServer::start().await;
    let emitter = TelemetryEmitter::new(
        reqwest::Client::new(),
        &server.uri(),
        "pymth_live_test".into(),
        "rust/0.0.1/dev".into(),
        EmitterConfig {
            batch_size: 999,
            flush_interval_ms: 60_000,
            buffer_cap: 3,
            ..Default::default()
        },
    );
    for index in 0..5 {
        emitter
            .emit(
                "request.mint_started",
                EmitOptions {
                    correlation_id: Some(format!("c-{index}")),
                    ..Default::default()
                },
            )
            .await;
    }
    assert_eq!(emitter.buffer_size().await, 3);
    assert_eq!(emitter.dropped().await, 2);
    emitter.close().await;
}
