use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, SessionAxes, SessionBalance, SessionHandle, SessionRunner,
    SessionRunnerOptions,
};
use serde_json::json;
use tokio::sync::Mutex;
use wiremock::matchers::{body_partial_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const SID: &str = "11111111-1111-1111-1111-111111111111";

fn handle(broker: &MockServer, refill: &str) -> SessionHandle {
    SessionHandle {
        session_id: SID.to_string(),
        request_id: "open-request".to_string(),
        work_id: "wid".to_string(),
        broker_url: broker.uri(),
        protocol: "paid-session/v1".to_string(),
        capability: "livepeer:test".to_string(),
        offering: "default".to_string(),
        session: SessionAxes {
            descriptor_schema: "livepeer-session-test/v1".to_string(),
            attachment: "external".to_string(),
            metering: "runner-reported".to_string(),
            refill: refill.to_string(),
        },
        session_params: json!({"room": "alpha"}),
        payment_envelope: "OPEN-ENV".to_string(),
        expected_value_wei: 100_000,
        funded_value_wei: 100_000,
        refill_endpoint: format!("/v1/sessions/{SID}/refill"),
        close_endpoint: format!("/v1/sessions/{SID}/close"),
        opened_at: "2026-08-20T00:00:00Z".to_string(),
    }
}

fn balance(status: &str, refuse: bool) -> serde_json::Value {
    json!({
        "status": status, "claimed_units": 80u64, "debited_units": 80u64,
        "unit": "participant_minutes", "runway_units": 20u64,
        "runway_seconds_estimate": 1200u64, "will_refuse_next_refill": refuse
    })
}

async fn mount_open(broker: &MockServer, schema: &str) {
    Mock::given(method("POST"))
        .and(path("/v1/session"))
        .and(header("Livepeer-Protocol", "paid-session/v1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "session_id": "broker-session", "work_id": "wid", "state": "active",
            "runtime": {"schema": schema, "public": {}, "grants": []},
            "credential": "credential", "lease": {"expires_at": "2026-08-21T00:00:00Z"},
            "balance": balance("ok", false),
            "control": {
                "status_url": format!("{}/status", broker.uri()),
                "topup_url": format!("{}/topup", broker.uri()),
                "end_url": format!("{}/end", broker.uri())
            }
        })))
        .mount(broker)
        .await;
}

#[tokio::test]
async fn paid_session_v1_open_refill_and_close() {
    let broker = MockServer::start().await;
    mount_open(&broker, "livepeer-session-test/v1").await;
    let topup_attempts = Arc::new(AtomicUsize::new(0));
    let attempts = topup_attempts.clone();
    Mock::given(method("POST"))
        .and(path("/topup"))
        .and(header("Authorization", "Bearer credential"))
        .and(header("Livepeer-Request-Id", "refill-request"))
        .respond_with(move |_: &wiremock::Request| {
            if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
                ResponseTemplate::new(503)
            } else {
                ResponseTemplate::new(200).set_body_json(json!({"balance": balance("ok", false)}))
            }
        })
        .expect(2)
        .mount(&broker)
        .await;
    Mock::given(method("GET"))
        .and(path("/status"))
        .and(header("Authorization", "Bearer credential"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"state": "active"})))
        .expect(1)
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/end"))
        .respond_with(ResponseTemplate::new(204).insert_header(
            "Livepeer-Settlement",
            "eyJwYXlsb2FkIjp7fSwic2lnbmF0dXJlIjp7fX0=",
        ))
        .expect(1)
        .mount(&broker)
        .await;

    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "wid",
            "request_id": "refill-request", "refill_seq": 1u64,
            "payment_envelope": "REFILL-ENV", "expected_value_wei": 50_000u64,
            "funded_value_wei": 50_000u64, "cap_status": null
        })))
        .expect(1)
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "outcome": "EXACT", "billed_value_wei": 150_000u64, "refund_wei": 0u64
        })))
        .mount(&loc)
        .await;

    let client = Client::new(ClientOptions::new(loc.uri(), "pymth_test")).unwrap();
    let first = SessionRunner::start(SessionRunnerOptions::new(
        client.clone(),
        handle(&broker, "extensible"),
    ))
    .await
    .unwrap();
    drop(first);
    let runner = SessionRunner::start(SessionRunnerOptions::new(
        client,
        handle(&broker, "extensible"),
    ))
    .await
    .unwrap();
    assert_eq!(runner.status().await.unwrap()["state"], "active");
    runner
        .on_balance(serde_json::from_value(balance("low", false)).unwrap())
        .await;
    runner
        .on_balance(serde_json::from_value(balance("low", false)).unwrap())
        .await;
    let outcome = runner.close(150).await.unwrap();
    assert_eq!(outcome.outcome, "EXACT");
    assert_eq!(topup_attempts.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn recipient_rotation_uses_fresh_intent_and_declared_rebind() {
    let broker = MockServer::start().await;
    mount_open(&broker, "livepeer-session-test/v1").await;
    Mock::given(method("POST"))
        .and(path("/topup"))
        .and(header("Livepeer-Payment", "OLD"))
        .respond_with(
            ResponseTemplate::new(409).insert_header("Livepeer-Error", "recipient_rotated"),
        )
        .expect(1)
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/topup"))
        .and(header("Livepeer-Payment", "NEW"))
        .and(header("Livepeer-Request-Id", "replacement"))
        .and(header("Livepeer-Rebind-From", "wid"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"balance": balance("ok", false)})),
        )
        .expect(1)
        .mount(&broker)
        .await;

    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/refill")))
        .and(body_partial_json(json!({"rebind_from": "wid"})))
        .and(body_partial_json(
            json!({"replaces_request_id": "rejected"}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "successor", "request_id": "replacement",
            "payment_envelope": "NEW", "rebind_from": "wid"
        })))
        .expect(1)
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "wid", "request_id": "rejected", "payment_envelope": "OLD"
        })))
        .expect(1)
        .mount(&loc)
        .await;

    let client = Client::new(ClientOptions::new(loc.uri(), "pymth_test")).unwrap();
    let warnings = Arc::new(Mutex::new(Vec::new()));
    let captured = warnings.clone();
    let mut options = SessionRunnerOptions::new(client, handle(&broker, "extensible"));
    options.on_winddown_warning = Some(Arc::new(move |event| {
        let captured = captured.clone();
        Box::pin(async move { captured.lock().await.push(event.reason) })
    }));
    let runner = SessionRunner::start(options).await.unwrap();
    runner
        .on_balance(serde_json::from_value(balance("low", false)).unwrap())
        .await;
    assert_eq!(runner.broker_session().await.work_id, "successor");
    assert!(warnings.lock().await.is_empty());
}

#[tokio::test]
async fn declared_rebind_refusal_drains_once() {
    let broker = MockServer::start().await;
    mount_open(&broker, "livepeer-session-test/v1").await;
    Mock::given(method("POST"))
        .and(path("/topup"))
        .and(header("Livepeer-Payment", "OLD"))
        .respond_with(
            ResponseTemplate::new(409).insert_header("Livepeer-Error", "recipient_rotated"),
        )
        .expect(1)
        .mount(&broker)
        .await;
    Mock::given(method("POST"))
        .and(path("/topup"))
        .and(header("Livepeer-Payment", "NEW"))
        .and(header("Livepeer-Rebind-From", "wid"))
        .respond_with(ResponseTemplate::new(409).insert_header("Livepeer-Error", "rebind_refused"))
        .expect(1)
        .mount(&broker)
        .await;

    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/refill")))
        .and(body_partial_json(json!({"rebind_from": "wid"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "successor", "request_id": "replacement",
            "payment_envelope": "NEW", "rebind_from": "wid"
        })))
        .expect(1)
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{SID}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "work_id": "wid", "request_id": "rejected", "payment_envelope": "OLD"
        })))
        .expect(1)
        .mount(&loc)
        .await;

    let warnings = Arc::new(Mutex::new(Vec::new()));
    let captured = warnings.clone();
    let client = Client::new(ClientOptions::new(loc.uri(), "pymth_test")).unwrap();
    let mut options = SessionRunnerOptions::new(client, handle(&broker, "extensible"));
    options.on_winddown_warning = Some(Arc::new(move |event| {
        let captured = captured.clone();
        Box::pin(async move { captured.lock().await.push(event.reason) })
    }));
    let runner = SessionRunner::start(options).await.unwrap();
    runner
        .on_balance(serde_json::from_value(balance("low", false)).unwrap())
        .await;
    assert_eq!(*warnings.lock().await, ["payment_unrecoverable"]);
}

#[tokio::test]
async fn bounded_and_refusal_warning_balances_drain() {
    let broker = MockServer::start().await;
    mount_open(&broker, "livepeer-session-test/v1").await;
    let loc = MockServer::start().await;
    let warnings = Arc::new(Mutex::new(Vec::new()));
    let captured = warnings.clone();
    let client = Client::new(ClientOptions::new(loc.uri(), "pymth_test")).unwrap();
    let mut options = SessionRunnerOptions::new(client, handle(&broker, "bounded"));
    options.on_winddown_warning = Some(Arc::new(move |event| {
        let captured = captured.clone();
        Box::pin(async move { captured.lock().await.push(event.reason) })
    }));
    let runner = SessionRunner::start(options).await.unwrap();
    runner
        .on_balance(serde_json::from_value::<SessionBalance>(balance("low", false)).unwrap())
        .await;
    runner
        .on_balance(serde_json::from_value::<SessionBalance>(balance("ok", true)).unwrap())
        .await;
    assert_eq!(
        *warnings.lock().await,
        [
            "bounded_runway_exhausting",
            "broker_will_refuse_next_refill"
        ]
    );
}

#[tokio::test]
async fn descriptor_mismatch_fails_closed() {
    let broker = MockServer::start().await;
    mount_open(&broker, "wrong/v1").await;
    let loc = MockServer::start().await;
    let client = Client::new(ClientOptions::new(loc.uri(), "pymth_test")).unwrap();
    let result = SessionRunner::start(SessionRunnerOptions::new(
        client,
        handle(&broker, "extensible"),
    ))
    .await;
    assert!(result.is_err());
}
