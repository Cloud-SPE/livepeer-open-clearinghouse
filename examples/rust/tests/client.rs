//! Tests for the handoff-mode Rust SDK. Uses wiremock to stub both
//! the LOC gateway and the broker.

use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, ErrorKind, JobBody, OpenSessionInput, SubmitJobInput,
};
use serde_json::json;
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const API_KEY: &str = "pymth_live_test";

fn loc_client(loc: &MockServer) -> Client {
    Client::new(ClientOptions::new(loc.uri(), API_KEY)).unwrap()
}

fn job_open_payload(broker_url: &str) -> serde_json::Value {
    json!({
        "job_id": "00000000-0000-0000-0000-000000000abc",
        "work_id": "wid-abc",
        "broker_url": broker_url,
        "mode": "http-reqresp@v0",
        "payment_envelope": "BASE64ENV",
        "expected_value_wei": 100_000u64,
        "funded_value_wei": 100_000u64,
        "settle_endpoint": "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
        "opened_at": "2026-05-24T12:00:00Z"
    })
}

fn settled_payload(actual: u64) -> serde_json::Value {
    #[allow(clippy::cast_precision_loss)]
    let pct = actual as f64 / 100.0;
    json!({
        "job_id": "00000000-0000-0000-0000-000000000abc",
        "work_id": "wid-abc",
        "actual_units": actual,
        "billed_value_wei": actual * 1000,
        "refund_wei": 100_000u64 - actual * 1000,
        "outcome": "OVERFUNDED",
        "closed_at": "2026-05-24T12:00:30Z",
        "cap_status": {
            "session_pct_used": pct,
            "spend_period_pct_used": null,
            "user_balance_pct_used": null,
            "operator_pool_pct_used": null,
            "will_refuse_next_refill": false,
            "winddown_reason": null
        }
    })
}

#[test]
fn rejects_bad_api_key() {
    let err = Client::new(ClientOptions::new("https://x", "nope")).unwrap_err();
    assert!(matches!(
        err,
        livepeer_open_clearinghouse_sdk::OpenClearinghouseError::Config(_)
    ));
}

#[tokio::test]
async fn submit_job_happy_path() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let broker_uri = broker.uri();

    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .and(header("Livepeer-Open-Clearinghouse-SDK", "rust/0.2.0/dev"))
        .respond_with(ResponseTemplate::new(201).set_body_json(job_open_payload(&broker_uri)))
        .mount(&loc)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/cap"))
        .and(header("Livepeer-Payment", "BASE64ENV"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "42"),
        )
        .mount(&broker)
        .await;

    Mock::given(method("POST"))
        .and(path(
            "/v1/jobs/00000000-0000-0000-0000-000000000abc/settle",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(42)))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let result = client
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 80,
            max_total_units: Some(100),
            body: JobBody::Json(json!({"prompt": "hello"})),
            request_id: None,
            spec_version: None,
        })
        .await
        .expect("submit_job");

    assert_eq!(result.status, 200);
    assert_eq!(result.actual_units, 42);
    assert_eq!(result.billed_value_wei, 42_000);
    assert_eq!(result.refund_wei, 58_000);
    assert_eq!(result.outcome, "OVERFUNDED");
    assert_eq!(result.body, Some(json!({"reply": "ok"})));
    assert!(result.cap_status.session_pct_used > 0.4);
}

#[tokio::test]
async fn submit_job_maps_insufficient_credit() {
    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(402).set_body_json(json!({
            "error": {
                "code": "INSUFFICIENT_CREDIT",
                "message": "broke",
                "details": {"available_wei": "0", "required_wei": "1000"}
            }
        })))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let err = client
        .submit_job(SubmitJobInput {
            capability: "x",
            offering: "x",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({})),
            request_id: None,
            spec_version: None,
        })
        .await
        .expect_err("expected error");
    assert_eq!(err.kind(), ErrorKind::InsufficientCredit);
}

#[tokio::test]
async fn open_session_returns_handle() {
    let loc = MockServer::start().await;
    let sid = "11111111-1111-1111-1111-111111111111";
    Mock::given(method("POST"))
        .and(path("/v1/sessions"))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "session_id": sid,
            "work_id": "wid-sess",
            "broker_url": "https://broker.example/livepeer",
            "mode": "session-control-plus-media@v0",
            "payment_envelope": "BASE64SESS",
            "expected_value_wei": 100_000u64,
            "funded_value_wei": 200_000u64,
            "refill_endpoint": format!("/v1/sessions/{sid}/refill"),
            "close_endpoint": format!("/v1/sessions/{sid}/close"),
            "opened_at": "2026-05-24T12:00:00Z"
        })))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let handle = client
        .open_session(OpenSessionInput {
            capability: "livepeer:vtuber-session",
            offering: "vtuber-1080p30",
            estimated_runway_units: 100,
            max_total_units: 200,
        })
        .await
        .expect("open_session");
    assert_eq!(handle.mode, "session-control-plus-media@v0");
    assert_eq!(handle.funded_value_wei, 200_000);
}

#[tokio::test]
async fn close_session_threads_outcome() {
    let loc = MockServer::start().await;
    let sid = "22222222-2222-2222-2222-222222222222";
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "session_id": sid,
            "work_id": "w",
            "actual_units": 100,
            "billed_value_wei": 100_000u64,
            "refund_wei": 0u64,
            "outcome": "EXACT",
            "closed_at": "2026-05-24T12:30:00Z"
        })))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let result = client
        .close_session(sid, 100, Some("EXACT"), None)
        .await
        .expect("close_session");
    assert_eq!(result.get("outcome").and_then(|v| v.as_str()), Some("EXACT"));
}

#[tokio::test]
async fn list_capabilities_unwraps_items() {
    let loc = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/v1/capabilities"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "items": [{
                "name": "openai:embeddings",
                "work_unit": "token",
                "offerings": []
            }]
        })))
        .mount(&loc)
        .await;
    let client = loc_client(&loc);
    let caps = client.list_capabilities().await.unwrap();
    assert_eq!(caps[0].name, "openai:embeddings");
}
