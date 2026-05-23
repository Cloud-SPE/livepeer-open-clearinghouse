use pymthouse_sdk::{Client, ClientOptions, ErrorKind, MintPaymentInput, PymtHouseError};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const KEY: &str = "pymth_live_test_key";

async fn server_client() -> (MockServer, Client) {
    let mock = MockServer::start().await;
    let client = Client::new(ClientOptions::new(mock.uri(), KEY)).expect("client");
    (mock, client)
}

#[tokio::test]
async fn mints_on_happy_path() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/payments/mint"))
        .and(header("x-api-key", KEY))
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "payment_id": "00000000-0000-0000-0000-000000000001",
            "work_id": "deadbeefdeadbeef",
            "payment_bytes": "AAAA",
            "expected_value_wei": "244140",
            "funded_value_wei": "25000000000",
            "recipient_eth_address": "0xd003"
        })))
        .mount(&mock)
        .await;

    let mint = client
        .mint_payment(MintPaymentInput {
            capability: "openai:chat-completions",
            offering: "vllm-qwen3.6-27b-default",
            work_units: 1000,
            idempotency_key: None,
        })
        .await
        .expect("mint");
    assert_eq!(mint.payment_bytes, "AAAA");
    assert_eq!(mint.recipient_eth_address, "0xd003");
}

#[tokio::test]
async fn insufficient_credit_maps_to_kind() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/payments/mint"))
        .respond_with(ResponseTemplate::new(402).set_body_json(serde_json::json!({
            "error": {
                "code": "INSUFFICIENT_CREDIT",
                "message": "0 < 1000",
                "details": { "available_wei": "0", "required_wei": "1000" }
            }
        })))
        .mount(&mock)
        .await;

    let err = client
        .mint_payment(MintPaymentInput {
            capability: "x",
            offering: "y",
            work_units: 1,
            idempotency_key: None,
        })
        .await
        .expect_err("should error");
    assert_eq!(err.kind(), ErrorKind::InsufficientCredit);
    if let PymtHouseError::Api {
        status, details, ..
    } = err
    {
        assert_eq!(status, 402);
        assert_eq!(details["required_wei"], "1000");
    } else {
        panic!("expected Api variant");
    }
}

#[tokio::test]
async fn rate_limited_carries_retry_after() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/payments/mint"))
        .respond_with(
            ResponseTemplate::new(429)
                .insert_header("Retry-After", "12")
                .set_body_json(serde_json::json!({ "detail": "rate_limited" })),
        )
        .mount(&mock)
        .await;
    let err = client
        .mint_payment(MintPaymentInput {
            capability: "x",
            offering: "y",
            work_units: 1,
            idempotency_key: None,
        })
        .await
        .expect_err("should error");
    assert_eq!(err.kind(), ErrorKind::RateLimited);
    assert_eq!(err.retry_after_seconds(), Some(12));
}

#[tokio::test]
async fn idempotency_key_threaded() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/payments/mint"))
        .and(header("Idempotency-Key", "abc-123"))
        .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
            "payment_id": "00000000-0000-0000-0000-000000000001",
            "work_id": "x",
            "payment_bytes": "AAAA",
            "expected_value_wei": "1",
            "funded_value_wei": "1",
            "recipient_eth_address": "0xd003"
        })))
        .mount(&mock)
        .await;
    client
        .mint_payment(MintPaymentInput {
            capability: "x",
            offering: "y",
            work_units: 1,
            idempotency_key: Some("abc-123"),
        })
        .await
        .expect("mint");
}

#[test]
fn rejects_bad_api_key() {
    let err =
        Client::new(ClientOptions::new("http://x", "not-a-real-key")).expect_err("should error");
    match err {
        PymtHouseError::Config(msg) => assert!(msg.contains("pymth_"), "{msg}"),
        other => panic!("expected Config, got {other:?}"),
    }
}

#[tokio::test]
async fn list_capabilities_unwraps_items() {
    let (mock, client) = server_client().await;
    Mock::given(method("GET"))
        .and(path("/v1/capabilities"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "items": [{"name": "openai:chat-completions", "work_unit": "tokens", "offerings": []}]
        })))
        .mount(&mock)
        .await;
    let caps = client.list_capabilities().await.expect("list");
    assert_eq!(caps.len(), 1);
    assert_eq!(caps[0].name, "openai:chat-completions");
}

#[tokio::test]
async fn list_orchestrators_passes_capability_filter() {
    let (mock, client) = server_client().await;
    Mock::given(method("GET"))
        .and(path("/v1/orchestrators"))
        // The query "capability=openai:chat-completions" becomes
        // "capability=openai%3Achat-completions" after our urlencoded()
        // helper.
        .and(wiremock::matchers::query_param(
            "capability",
            "openai:chat-completions",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"items": []})))
        .mount(&mock)
        .await;
    let orchs = client
        .list_orchestrators(Some("openai:chat-completions"))
        .await
        .expect("list");
    assert!(orchs.is_empty());
}

#[tokio::test]
async fn list_orchestrators_without_filter() {
    let (mock, client) = server_client().await;
    Mock::given(method("GET"))
        .and(path("/v1/orchestrators"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"items": []})))
        .mount(&mock)
        .await;
    let orchs = client.list_orchestrators(None).await.expect("list");
    assert!(orchs.is_empty());
}

#[tokio::test]
async fn report_usage_returns_refund() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/usage/report"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "refunded_wei": "12345",
            "payment_status": "settled",
            "new_balance_wei": "999999",
            "usage": {"id": "u1", "actual_work_units": 800, "final_charge_wei": "20000"}
        })))
        .mount(&mock)
        .await;
    let result = client
        .report_usage(pymthouse_sdk::ReportUsageInput {
            payment_id: "00000000-0000-0000-0000-000000000001",
            actual_work_units: 800,
            idempotency_key: Some("abc-123"),
        })
        .await
        .expect("report");
    assert_eq!(result.refunded_wei, "12345");
    assert_eq!(result.new_balance_wei, "999999");
}

#[tokio::test]
async fn non_json_error_body_falls_back_to_text() {
    let (mock, client) = server_client().await;
    Mock::given(method("POST"))
        .and(path("/v1/payments/mint"))
        .respond_with(ResponseTemplate::new(503).set_body_string("upstream down"))
        .mount(&mock)
        .await;
    let err = client
        .mint_payment(MintPaymentInput {
            capability: "x",
            offering: "y",
            work_units: 1,
            idempotency_key: None,
        })
        .await
        .expect_err("should error");
    if let PymtHouseError::Api { status, .. } = err {
        assert_eq!(status, 503);
    } else {
        panic!("expected Api variant; got {err:?}");
    }
}
