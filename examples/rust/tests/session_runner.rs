//! Tests for the Rust `SessionRunner`. Uses wiremock for LOC and an
//! in-process tokio-tungstenite server for the broker side.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt as _, StreamExt as _};
use livepeer_open_clearinghouse_sdk::{
    Client, ClientOptions, OpenClearinghouseError, SessionHandle, SessionRunner,
    SessionRunnerOptions,
};
use serde_json::json;
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const API_KEY: &str = "pymth_live_test";

fn loc_client(loc: &MockServer) -> Client {
    Client::new(ClientOptions::new(loc.uri(), API_KEY)).unwrap()
}

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

/// Spin up an in-process WebSocket server. Returns ws:// URL + the
/// listening task handle. Caller hands in a handler that drives one
/// connection.
async fn start_ws_server(
    handler: Arc<
        dyn Fn(
                tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
            ) -> futures_util::future::BoxFuture<'static, ()>
            + Send
            + Sync,
    >,
) -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr: SocketAddr = listener.local_addr().unwrap();
    let url = format!("ws://{addr}");
    let h = tokio::spawn(async move {
        if let Ok((stream, _)) = listener.accept().await {
            if let Ok(ws) = tokio_tungstenite::accept_async(stream).await {
                handler(ws).await;
            }
        }
    });
    (url, h)
}

fn refill_response() -> serde_json::Value {
    json!({
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
            "winddown_reason": null
        }
    })
}

fn close_response(sid: &str) -> serde_json::Value {
    json!({
        "session_id": sid,
        "work_id": "wid",
        "actual_units": 0u64,
        "billed_value_wei": 0u64,
        "refund_wei": 200_000u64,
        "outcome": "OVERFUNDED",
        "closed_at": "2026-05-24T12:30:00Z"
    })
}

#[tokio::test]
async fn refills_on_balance_low_for_session_control_plus_media() {
    let sid = "11111111-1111-1111-1111-111111111111";
    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/refill")))
        .respond_with(ResponseTemplate::new(200).set_body_json(refill_response()))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(close_response(sid)))
        .mount(&loc)
        .await;

    let received: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let recv_clone = received.clone();
    let (ws_url, _ws_task) = start_ws_server(Arc::new(move |mut ws| {
        let recv = recv_clone.clone();
        Box::pin(async move {
            let _ = ws
                .send(Message::Text(
                    r#"{"type":"session.balance.low","observed_consumed_units":80}"#.to_string(),
                ))
                .await;
            while let Some(msg) = ws.next().await {
                if let Ok(Message::Text(t)) = msg {
                    recv.lock().await.push(t);
                }
            }
        })
    }))
    .await;

    let client = loc_client(&loc);
    let runner = SessionRunner::start(SessionRunnerOptions::new(
        client,
        make_handle(ws_url, "session-control-plus-media@v0", sid),
    ))
    .await
    .unwrap();

    // Wait for the topup frame to round-trip
    timeout(Duration::from_secs(2), async {
        loop {
            if !received.lock().await.is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("timed out waiting for topup frame");

    let out = runner.close(0).await.unwrap();
    assert_eq!(out.outcome, "OVERFUNDED");

    let frames = received.lock().await.clone();
    assert_eq!(frames.len(), 1);
    let parsed: serde_json::Value = serde_json::from_str(&frames[0]).unwrap();
    assert_eq!(parsed["type"], "session.topup");
    assert_eq!(parsed["body"]["payment_header"], "REFILL-ENV");
}

#[tokio::test]
async fn ws_realtime_bounded_fires_winddown_only() {
    let sid = "22222222-2222-2222-2222-222222222222";
    let loc = MockServer::start().await;
    // No refill mount — if SDK calls it, it'll 404.
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(close_response(sid)))
        .mount(&loc)
        .await;

    let (ws_url, _ws_task) = start_ws_server(Arc::new(move |mut ws| {
        Box::pin(async move {
            let _ = ws
                .send(Message::Text(
                    r#"{"type":"session.balance.low"}"#.to_string(),
                ))
                .await;
            while ws.next().await.is_some() {}
        })
    }))
    .await;

    let client = loc_client(&loc);
    let wd_seen: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let wd_clone = wd_seen.clone();
    let mut opts = SessionRunnerOptions::new(
        client,
        make_handle(ws_url, "ws-realtime@v0", sid),
    );
    opts.on_winddown_warning = Some(Arc::new(move |e| {
        let wd = wd_clone.clone();
        Box::pin(async move {
            *wd.lock().await = Some(e.reason);
        })
    }));
    let runner = SessionRunner::start(opts).await.unwrap();

    timeout(Duration::from_secs(2), async {
        loop {
            if wd_seen.lock().await.is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("timed out waiting for winddown");

    let _ = runner.close(0).await.unwrap();
    assert_eq!(wd_seen.lock().await.clone().unwrap(), "ws_session_exhausting");
}

#[tokio::test]
async fn refill_402_routes_to_on_refill_refused() {
    let sid = "33333333-3333-3333-3333-333333333333";
    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/refill")))
        .respond_with(ResponseTemplate::new(402).set_body_json(json!({
            "error": {"code": "cap_reached", "message": "period cap reached",
                      "details": {"which": "spend_period"}}
        })))
        .mount(&loc)
        .await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(close_response(sid)))
        .mount(&loc)
        .await;

    let (ws_url, _ws_task) = start_ws_server(Arc::new(move |mut ws| {
        Box::pin(async move {
            let _ = ws
                .send(Message::Text(
                    r#"{"type":"session.balance.low"}"#.to_string(),
                ))
                .await;
            while ws.next().await.is_some() {}
        })
    }))
    .await;

    let client = loc_client(&loc);
    let refused: Arc<Mutex<bool>> = Arc::new(Mutex::new(false));
    let r_clone = refused.clone();
    let mut opts = SessionRunnerOptions::new(
        client,
        make_handle(ws_url, "session-control-plus-media@v0", sid),
    );
    opts.on_refill_refused = Some(Arc::new(move |e| {
        let r = r_clone.clone();
        Box::pin(async move {
            assert!(e.error.is_some());
            *r.lock().await = true;
        })
    }));
    let runner = SessionRunner::start(opts).await.unwrap();

    timeout(Duration::from_secs(2), async {
        loop {
            if *refused.lock().await {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("timed out waiting for refused");

    let _ = runner.close(0).await.unwrap();
    assert!(*refused.lock().await);
}

#[tokio::test]
async fn unsupported_mode_raises_at_start() {
    let loc = MockServer::start().await;
    let client = loc_client(&loc);
    let res = SessionRunner::start(SessionRunnerOptions::new(
        client,
        make_handle(
            "http://broker.test".to_string(),
            "http-reqresp@v0",
            "44444444-4444-4444-4444-444444444444",
        ),
    ))
    .await;
    let Err(err) = res else { panic!("expected error") };
    assert!(matches!(err, OpenClearinghouseError::Config(ref m) if m.contains("unsupported mode")));
}

#[tokio::test]
async fn close_is_idempotent() {
    let sid = "55555555-5555-5555-5555-555555555555";
    let loc = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path(format!("/v1/sessions/{sid}/close")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "session_id": sid, "work_id": "wid",
            "actual_units": 80u64, "billed_value_wei": 80_000u64,
            "refund_wei": 120_000u64, "outcome": "OVERFUNDED",
            "closed_at": "2026-05-24T12:30:00Z"
        })))
        .expect(1)
        .mount(&loc)
        .await;

    let (ws_url, _ws_task) = start_ws_server(Arc::new(move |mut ws| {
        Box::pin(async move {
            while ws.next().await.is_some() {}
        })
    }))
    .await;

    let client = loc_client(&loc);
    let runner = SessionRunner::start(SessionRunnerOptions::new(
        client,
        make_handle(ws_url, "session-control-plus-media@v0", sid),
    ))
    .await
    .unwrap();
    let a = runner.close(80).await.unwrap();
    let b = runner.close(80).await.unwrap();
    assert_eq!(a.outcome, "OVERFUNDED");
    assert_eq!(a.billed_value_wei, 80_000);
    assert_eq!(a.refund_wei, 120_000);
    assert_eq!(b.outcome, a.outcome);
}
