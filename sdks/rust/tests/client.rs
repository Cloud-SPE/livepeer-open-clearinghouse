//! Tests for the handoff-mode Rust SDK. Uses wiremock to stub both
//! the LOC gateway and the broker.

use base64::Engine as _;
use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, ErrorKind, JobBody, OpenSessionInput, SubmitJobInput,
};
use serde_json::json;
use wiremock::matchers::{body_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const API_KEY: &str = "pymth_live_test";
const ENCODED_SETTLEMENT: &str = "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0=";

fn loc_client(loc: &MockServer) -> Client {
    Client::new(ClientOptions::new(loc.uri(), API_KEY)).unwrap()
}

fn job_open_payload(broker_url: &str) -> serde_json::Value {
    json!({
        "job_id": "00000000-0000-0000-0000-000000000abc",
        "request_id": "broker-request-1",
        "work_id": "wid-abc",
        "broker_url": broker_url,
        "protocol": "paid-job/v1",
        "transport": "unary",
        "work_unit": "token",
        "payment_envelope": "BASE64ENV",
        "expected_value_wei": "100000",
        "funded_value_wei": "100000",
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
        "billed_value_wei": (actual * 1000).to_string(),
        "refund_wei": (100_000u64 - actual * 1000).to_string(),
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
        .and(header("Livepeer-Open-Clearinghouse-SDK", "rust/2.0.0/dev"))
        .and(header("Idempotency-Key", "loc-id-1"))
        .respond_with(ResponseTemplate::new(201).set_body_json(job_open_payload(&broker_uri)))
        .mount(&loc)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .and(header("Livepeer-Payment", "BASE64ENV"))
        .and(header("Livepeer-Protocol", "paid-job/v1"))
        .and(header("Livepeer-Request-Id", "broker-request-1"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "42")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;

    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .and(body_json(json!({
            "actual_units": 42,
            "broker_job_id": "broker-job-1",
            "work_unit": "token",
            "settlement": {"payload": {}, "signature": {}}
        })))
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
            request_id: Some("loc-id-1".to_string()),
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit_job");

    assert_eq!(result.status, 200);
    assert_eq!(result.actual_units, 42);
    assert_eq!(result.billed_value_wei, 42_000);
    assert_eq!(result.refund_wei, 58_000);
    assert_eq!(result.outcome, "OVERFUNDED");
    assert_eq!(result.body, Some(json!({"reply": "ok"})));
    assert_eq!(result.protocol, "paid-job/v1");
    assert_eq!(result.transport, "unary");
    assert_eq!(result.work_unit, "token");
    assert_eq!(result.broker_job_id, "broker-job-1");
    assert_eq!(result.request_id, "broker-request-1");
    assert!(result.cap_status.session_pct_used > 0.4);
}

#[tokio::test]
async fn submit_job_stream_queries_terminal_claim_and_settlement() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let mut open = job_open_payload(&broker.uri());
    open["transport"] = json!("stream");
    let settlement = json!({
        "payload": {"work_id": "wid-abc", "debited_units": "7"},
        "signature": {
            "algorithm": "secp256k1",
            "canonicalization": "jcs",
            "value": "0xsigned"
        }
    });
    let encoded =
        base64::engine::general_purpose::STANDARD.encode(serde_json::to_vec(&settlement).unwrap());

    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(open))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .and(header("Accept", "text/event-stream"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_string("data: hello\n\n")
                .insert_header("Content-Type", "text/event-stream")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Work-Unit", "token"),
        )
        .mount(&broker)
        .await;
    Mock::given(method("GET"))
        .and(path("/v1/settlement/broker-job-1"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"job_id": "broker-job-1", "state": "terminal"}))
                .insert_header("Livepeer-Work-Units", "7")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", encoded.as_str()),
        )
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .and(body_json(json!({
            "actual_units": 7,
            "broker_job_id": "broker-job-1",
            "work_unit": "token",
            "settlement": settlement
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(7)))
        .mount(&loc)
        .await;

    let result = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 10,
            max_total_units: None,
            body: JobBody::Json(json!({"prompt": "hello"})),
            request_id: None,
            transport: Some("stream"),
            content_type: None,
        })
        .await
        .expect("stream submit");

    assert_eq!(result.body_text, "data: hello\n\n");
    assert_eq!(result.actual_units, 7);
    assert_eq!(result.transport, "stream");
    assert_eq!(result.broker_job_id, "broker-job-1");
}

#[tokio::test]
async fn submit_job_multipart_selects_declared_transport() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let mut open = job_open_payload(&broker.uri());
    open["transport"] = json!("multipart");
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(open))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .and(header(
            "Content-Type",
            "multipart/form-data; boundary=boundary",
        ))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"ok": true}))
                .insert_header("Livepeer-Work-Units", "2")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(2)))
        .mount(&loc)
        .await;
    let result = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "x",
            offering: "x",
            estimated_units: 2,
            max_total_units: None,
            body: JobBody::Bytes(b"--boundary--"),
            request_id: None,
            transport: Some("multipart"),
            content_type: Some("multipart/form-data; boundary=boundary"),
        })
        .await
        .expect("multipart submit");
    assert_eq!(result.transport, "multipart");
}

#[tokio::test]
async fn submit_job_rejects_work_unit_drift() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(job_open_payload(&broker.uri())))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({}))
                .insert_header("Livepeer-Work-Units", "3")
                .insert_header("Livepeer-Work-Unit", "frames")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;
    let err = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "x",
            offering: "x",
            estimated_units: 3,
            max_total_units: None,
            body: JobBody::Json(json!({})),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect_err("unit drift must fail");
    assert!(matches!(
        err,
        livepeer_open_clearinghouse_sdk::OpenClearinghouseError::BrokerProtocol { ref code, .. }
            if code == "work_unit_mismatch"
    ));
}

#[tokio::test]
async fn submit_job_terminal_error_settles_zero() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(job_open_payload(&broker.uri())))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .respond_with(
            ResponseTemplate::new(429)
                .set_body_json(json!({"error": "rate_limited"}))
                .insert_header("Livepeer-Work-Units", "0")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .and(body_json(json!({
            "actual_units": 0,
            "broker_job_id": "broker-job-1",
            "work_unit": "token",
            "settlement": {"payload": {}, "signature": {}}
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(0)))
        .mount(&loc)
        .await;
    let result = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "x",
            offering: "x",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({})),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("terminal error result");
    assert_eq!(result.status, 429);
    assert_eq!(result.actual_units, 0);
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
            transport: None,
            content_type: None,
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
            "request_id": "req-session",
            "protocol": "paid-session/v1",
            "session": {
                "descriptor_schema": "livepeer-session-test/v1",
                "attachment": "external", "metering": "runner-reported", "refill": "extensible"
            },
            "payment_envelope": "BASE64SESS",
            "expected_value_wei": "100000",
            "funded_value_wei": "200000",
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
            descriptor_schema: "livepeer-session-test/v1",
            session_params: json!({}),
            estimated_runway_units: 100,
            max_total_units: 200,
            request_id: None,
        })
        .await
        .expect("open_session");
    assert_eq!(handle.protocol, "paid-session/v1");
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
            "billed_value_wei": "100000",
            "refund_wei": "0",
            "outcome": "EXACT",
            "closed_at": "2026-05-24T12:30:00Z"
        })))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let result = client
        .close_session(
            sid,
            100,
            Some("EXACT"),
            json!({"payload": {}, "signature": {}}),
        )
        .await
        .expect("close_session");
    assert_eq!(
        result.get("outcome").and_then(|v| v.as_str()),
        Some("EXACT")
    );
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

// ---- model injection from the route (openai:* capabilities) ----

fn job_open_with_route_model(broker_url: &str, model: Option<&str>) -> serde_json::Value {
    let mut open = job_open_payload(broker_url);
    let openai = model.map_or_else(|| json!({}), |m| json!({ "model": m }));
    open["route_snapshot"] = json!({
        "worker_url": broker_url,
        "price_per_work_unit_wei": "1000",
        "extra": { "openai": openai }
    });
    open
}

async fn mount_broker_and_settle(
    loc: &MockServer,
    broker: &MockServer,
    expected_body: serde_json::Value,
) {
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .and(body_json(expected_body))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "1")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .expect(1)
        .mount(broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(1)))
        .mount(loc)
        .await;
}

#[tokio::test]
async fn submit_job_injects_model_from_route_for_openai() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(job_open_with_route_model(
                &broker.uri(),
                Some("qwen3.6-27b"),
            )),
        )
        .mount(&loc)
        .await;
    mount_broker_and_settle(
        &loc,
        &broker,
        json!({ "messages": [{ "role": "user", "content": "hi" }], "model": "qwen3.6-27b" }),
    )
    .await;

    let result = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "qwen3.6-27b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({ "messages": [{ "role": "user", "content": "hi" }] })),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit with injected model");
    assert_eq!(result.actual_units, 1);
}

#[tokio::test]
async fn submit_job_keeps_caller_supplied_model() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(job_open_with_route_model(
                &broker.uri(),
                Some("qwen3.6-27b"),
            )),
        )
        .mount(&loc)
        .await;
    mount_broker_and_settle(
        &loc,
        &broker,
        json!({ "prompt": "hi", "model": "my-model" }),
    )
    .await;

    loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "qwen3.6-27b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({ "prompt": "hi", "model": "my-model" })),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit with caller model");
}

#[tokio::test]
async fn submit_job_does_not_inject_model_for_non_openai_capability() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(
            ResponseTemplate::new(201)
                .set_body_json(job_open_with_route_model(&broker.uri(), Some("vod-model"))),
        )
        .mount(&loc)
        .await;
    mount_broker_and_settle(&loc, &broker, json!({ "schema": "video-transcode-vod/v2" })).await;

    loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "video:transcode.vod",
            offering: "vod-default",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({ "schema": "video-transcode-vod/v2" })),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit without injection");
}

#[tokio::test]
async fn submit_job_leaves_bytes_body_untouched() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(job_open_with_route_model(
                &broker.uri(),
                Some("qwen3.6-27b"),
            )),
        )
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .and(wiremock::matchers::body_bytes(
            b"{\"prompt\":\"raw\"}".to_vec(),
        ))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "1")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .expect(1)
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled_payload(1)))
        .mount(&loc)
        .await;

    loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "qwen3.6-27b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Bytes(b"{\"prompt\":\"raw\"}"),
            request_id: None,
            transport: None,
            content_type: Some("application/json"),
        })
        .await
        .expect("bytes body submit");
}

#[tokio::test]
async fn submit_job_skips_injection_when_route_has_no_model() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(
            ResponseTemplate::new(201)
                .set_body_json(job_open_with_route_model(&broker.uri(), Some(""))),
        )
        .mount(&loc)
        .await;
    mount_broker_and_settle(&loc, &broker, json!({ "prompt": "hi" })).await;

    loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "qwen3.6-27b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({ "prompt": "hi" })),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit with empty route model");
}

/// Every `*_wei` field on the wire is a decimal integer string that may
/// exceed `u64`; the SDK must carry it as an exact `u128`. This value sits
/// past 2^53, where a JSON-number decoder would already have lost digits.
const BIG_WEI: &str = "12345678901234567890";
const BIG_WEI_U128: u128 = 12_345_678_901_234_567_890;

fn telemetry_bodies(requests: &[wiremock::Request]) -> String {
    use std::io::Read as _;
    requests
        .iter()
        .filter(|r| r.url.path() == "/v1/telemetry")
        .map(|r| {
            let gzipped = r
                .headers
                .get("content-encoding")
                .is_some_and(|v| v.to_str().unwrap_or("").contains("gzip"));
            if gzipped {
                let mut out = String::new();
                flate2::read::GzDecoder::new(r.body.as_slice())
                    .read_to_string(&mut out)
                    .expect("gunzip telemetry body");
                out
            } else {
                String::from_utf8_lossy(&r.body).to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[tokio::test]
async fn wei_fields_beyond_u64_round_trip_exactly() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let mut open = job_open_payload(&broker.uri());
    open["expected_value_wei"] = json!(BIG_WEI);
    open["funded_value_wei"] = json!(BIG_WEI);
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(open))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "1")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;
    let mut settled = settled_payload(1);
    settled["billed_value_wei"] = json!(BIG_WEI);
    settled["refund_wei"] = json!(u128::MAX.to_string());
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/telemetry"))
        .respond_with(ResponseTemplate::new(202))
        .mount(&loc)
        .await;

    let client = loc_client(&loc);
    let result = client
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({"prompt": "hello"})),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit_job");
    assert_eq!(result.billed_value_wei, BIG_WEI_U128);
    assert_eq!(result.refund_wei, u128::MAX);
    assert!(result.refund_wei > u128::from(u64::MAX));
    assert_eq!(result.billed_value_wei.to_string(), BIG_WEI);
    client.close().await;

    // Telemetry payloads carry the same string form, never a JSON number.
    let bodies = telemetry_bodies(&loc.received_requests().await.unwrap_or_default());
    assert!(
        bodies.contains(&format!("\"billed_value_wei\":\"{BIG_WEI}\"")),
        "telemetry should carry billed_value_wei as a string: {bodies}"
    );
    assert!(
        bodies.contains(&format!("\"funded_value_wei\":\"{BIG_WEI}\"")),
        "telemetry should carry funded_value_wei as a string: {bodies}"
    );
    assert!(!bodies.contains(&format!("\"billed_value_wei\":{BIG_WEI}")));
}

#[tokio::test]
async fn legacy_bare_number_wei_fields_still_parse() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let mut open = job_open_payload(&broker.uri());
    open["expected_value_wei"] = json!(100_000u64);
    open["funded_value_wei"] = json!(100_000u64);
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(open))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path("/v1/job"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({ "reply": "ok" }))
                .insert_header("Livepeer-Work-Units", "42")
                .insert_header("Livepeer-Work-Unit", "token")
                .insert_header("Livepeer-Job-Id", "broker-job-1")
                .insert_header("Livepeer-Settlement", ENCODED_SETTLEMENT),
        )
        .mount(&broker)
        .await;
    let mut settled = settled_payload(42);
    settled["billed_value_wei"] = json!(42_000u64);
    settled["refund_wei"] = json!(58_000u64);
    Mock::given(method("POST"))
        .and(path("/v1/jobs/00000000-0000-0000-0000-000000000abc/settle"))
        .respond_with(ResponseTemplate::new(200).set_body_json(settled))
        .mount(&loc)
        .await;

    let result = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 42,
            max_total_units: None,
            body: JobBody::Json(json!({"prompt": "hello"})),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect("submit_job with legacy numeric wei");
    assert_eq!(result.billed_value_wei, 42_000);
    assert_eq!(result.refund_wei, 58_000);
}

#[tokio::test]
async fn open_session_parses_wei_beyond_u64() {
    let loc = MockServer::start().await;
    let sid = "33333333-3333-3333-3333-333333333333";
    Mock::given(method("POST"))
        .and(path("/v1/sessions"))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({
            "session_id": sid,
            "work_id": "wid-sess",
            "broker_url": "https://broker.example/livepeer",
            "request_id": "req-session",
            "protocol": "paid-session/v1",
            "session": {
                "descriptor_schema": "livepeer-session-test/v1",
                "attachment": "external", "metering": "runner-reported", "refill": "extensible"
            },
            "payment_envelope": "BASE64SESS",
            "expected_value_wei": BIG_WEI,
            "funded_value_wei": BIG_WEI,
            "refill_endpoint": format!("/v1/sessions/{sid}/refill"),
            "close_endpoint": format!("/v1/sessions/{sid}/close"),
            "opened_at": "2026-05-24T12:00:00Z"
        })))
        .mount(&loc)
        .await;

    let handle = loc_client(&loc)
        .open_session(OpenSessionInput {
            capability: "livepeer:vtuber-session",
            offering: "vtuber-1080p30",
            descriptor_schema: "livepeer-session-test/v1",
            session_params: json!({}),
            estimated_runway_units: 100,
            max_total_units: 200,
            request_id: None,
        })
        .await
        .expect("open_session");
    assert_eq!(handle.expected_value_wei, BIG_WEI_U128);
    assert_eq!(handle.funded_value_wei, BIG_WEI_U128);
}

#[tokio::test]
async fn submit_job_rejects_malformed_wei_string() {
    let loc = MockServer::start().await;
    let broker = MockServer::start().await;
    let mut open = job_open_payload(&broker.uri());
    open["funded_value_wei"] = json!("1e18");
    Mock::given(method("POST"))
        .and(path("/v1/jobs"))
        .respond_with(ResponseTemplate::new(201).set_body_json(open))
        .mount(&loc)
        .await;

    let err = loc_client(&loc)
        .submit_job(SubmitJobInput {
            capability: "openai:chat-completions",
            offering: "gpt-oss-20b",
            estimated_units: 1,
            max_total_units: None,
            body: JobBody::Json(json!({"prompt": "hello"})),
            request_id: None,
            transport: None,
            content_type: None,
        })
        .await
        .expect_err("non-integer wei string must not parse");
    assert!(
        err.to_string().contains("wei"),
        "error should name the wei field contract: {err}"
    );
}
