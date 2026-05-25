//! Coverage-focused tests for `SessionRunner` paths the happy-path
//! suite doesn't hit: HTTP-topup mode (live-session-*), explicit
//! `on_balance_low()` triggering for HTTP-topup, `outcome()` getter,
//! and the winddown path when `cap_status.will_refuse_next_refill` is
//! set.

use std::sync::Arc;
use std::time::Duration;

use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, EmitOptions, EmitterConfig, SessionHandle, SessionRunner,
    SessionRunnerOptions, TelemetryEmitter, WinddownEvent,
};
use serde_json::json;
use tokio::sync::Mutex;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const API_KEY: &str = "pymth_live_test";

fn make_handle(broker_url: String, mode: &str, sid: &str) -> SessionHandle {
    SessionHandle {
        session_id: sid.to_string(),
        work_id: "wid".to_string(),
        broker_url,
        mode: mode.to_string(),
        payment_envelope: "BASE64ENV".to_string(),
        expected_value_wei: 100_000,
        funded_value_wei: 200_000,
        refill_endpoint: format!("/v1/sessions/{sid}/refill"),
        close_endpoint: format!("/v1/sessions/{sid}/close"),
        opened_at: "2026-05-24T12:00:00Z".to_string(),
    }
}

fn loc_client(uri: &str) -> Client {
    Client::new(ClientOptions::new(uri, API_KEY)).unwrap()
}

#[tokio::test]
async fn http_topup_mode_open_live_session_captures_topup_url() {
    // For (d-extensible) HTTP-topup modes the SessionRunner opens
    // the broker session via POST /v1/cap and stashes the
    // control.topup_url returned in the body.
    let sid = "aaaaaaaa-1111-2222-3333-444444444444";
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/cap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "control": {"topup_url": format!("{}/topup", broker.uri())},
        })))
        .mount(&broker)
        .await;

    let loca = MockServer::start().await;
    // Pre-stage a refill response for the upcoming on_balance_low call.
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "wid",
            "refill_seq": 1u64,
            "payment_envelope": "REFILL-ENV",
            "expected_value_wei": 50_000u64,
            "funded_value_wei": 50_000u64,
            "cap_status": {
                "session_pct_used": 0.4,
                "spend_period_pct_used": null,
                "user_balance_pct_used": null,
                "operator_pool_pct_used": null,
                "will_refuse_next_refill": false,
                "winddown_reason": null,
            }
        })))
        .mount(&loca)
        .await;
    // The topup URL is on the broker; mount a 200 there.
    Mock::given(method("POST"))
        .and(path("/topup"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "session_id": sid,
            "work_id": "wid",
            "actual_units": 0u64,
            "billed_value_wei": 0u64,
            "refund_wei": 200_000u64,
            "outcome": "OVERFUNDED",
            "closed_at": "2026-05-25T12:30:00Z"
        })))
        .mount(&loca)
        .await;

    let client = loc_client(&loca.uri());
    let runner = SessionRunner::start(SessionRunnerOptions::new(
        client,
        make_handle(broker.uri(), "live-session-remote-runner@v0", sid),
    ))
    .await
    .unwrap();

    // Before close — outcome is None.
    assert!(runner.outcome().await.is_none());

    // Trigger on_balance_low() explicitly — HTTP-topup mode uses the
    // captured topup_url + the LOC refill envelope to POST.
    runner.on_balance_low(Some(80), None).await;

    let out = runner.close(0).await.unwrap();
    assert_eq!(out.outcome, "OVERFUNDED");
    // After close — outcome is Some.
    assert!(runner.outcome().await.is_some());
}

#[tokio::test]
async fn cap_imminent_fires_winddown_callback() {
    // When the refill response carries
    // cap_status.will_refuse_next_refill=true, the runner emits a
    // winddown event in addition to the success callback.
    let sid = "bbbbbbbb-5555-6666-7777-888888888888";
    let loca = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "wid",
            "refill_seq": 1u64,
            "payment_envelope": "REFILL-ENV",
            "expected_value_wei": 50_000u64,
            "funded_value_wei": 50_000u64,
            "cap_status": {
                "session_pct_used": 0.99,
                "spend_period_pct_used": null,
                "user_balance_pct_used": null,
                "operator_pool_pct_used": null,
                "will_refuse_next_refill": true,
                "winddown_reason": "session_cap_imminent",
            }
        })))
        .mount(&loca)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "session_id": sid,
            "work_id": "wid",
            "actual_units": 0u64,
            "billed_value_wei": 0u64,
            "refund_wei": 200_000u64,
            "outcome": "OVERFUNDED",
            "closed_at": "2026-05-25T12:30:00Z"
        })))
        .mount(&loca)
        .await;
    // Topup HTTP endpoint — broker side.
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/cap"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "control": {"topup_url": format!("{}/topup", broker.uri())},
        })))
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/topup"))
        .respond_with(ResponseTemplate::new(200))
        .mount(&broker)
        .await;

    let client = loc_client(&loca.uri());
    let winddown_reason: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let wd_clone = winddown_reason.clone();
    let mut opts = SessionRunnerOptions::new(
        client,
        make_handle(broker.uri(), "live-session-remote-runner@v0", sid),
    );
    opts.on_winddown_warning = Some(Arc::new(move |e: WinddownEvent| {
        let wd = wd_clone.clone();
        Box::pin(async move {
            *wd.lock().await = Some(e.reason);
        })
    }));
    let runner = SessionRunner::start(opts).await.unwrap();
    runner.on_balance_low(None, None).await;

    // Brief yield to let the callback fire.
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(
        winddown_reason.lock().await.clone().as_deref(),
        Some("session_cap_imminent")
    );
    let _ = runner.close(0).await.unwrap();
}

#[tokio::test]
async fn http_topup_open_fails_when_broker_returns_5xx() {
    // The open_live_session error path: broker session-open returns
    // a non-2xx → start() fails with OpenClearinghouseError.
    let sid = "cccccccc-9999-aaaa-bbbb-cccccccccccc";
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/cap"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&broker)
        .await;
    let loca = MockServer::start().await;
    let client = loc_client(&loca.uri());
    let res = SessionRunner::start(SessionRunnerOptions::new(
        client,
        make_handle(broker.uri(), "live-session-remote-runner@v0", sid),
    ))
    .await;
    assert!(res.is_err());
}

#[tokio::test]
async fn telemetry_emit_after_close_is_silent() {
    // Smoke for the closed-flag short-circuit in TelemetryEmitter.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .mount(&server)
        .await;
    let em: Arc<TelemetryEmitter> = TelemetryEmitter::new(
        reqwest::Client::new(),
        &server.uri(),
        "pymth_live_test".into(),
        "rust/0.0.1/dev".into(),
        EmitterConfig {
            flush_interval_ms: 60_000,
            ..Default::default()
        },
    );
    em.close().await;
    // After close, emit silently no-ops; buffer_size stays 0.
    em.emit("post.close", EmitOptions::default()).await;
    assert_eq!(em.buffer_size().await, 0);
    assert_eq!(em.dropped().await, 0);
}

#[tokio::test]
async fn telemetry_buffer_overflow_drops_oldest() {
    // Exercise the buffer-cap path: emit cap_bytes+1 events with a
    // huge cap; drops increment.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .mount(&server)
        .await;
    let em = TelemetryEmitter::new(
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
    for i in 0..5 {
        em.emit(
            "request.mint_started",
            EmitOptions {
                correlation_id: Some(format!("c-{i}")),
                ..Default::default()
            },
        )
        .await;
    }
    assert_eq!(em.buffer_size().await, 3);
    assert_eq!(em.dropped().await, 2);
    em.close().await;
}
