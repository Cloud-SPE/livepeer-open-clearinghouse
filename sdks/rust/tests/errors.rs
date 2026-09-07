//! Exhaustive coverage for the code -> kind mapping + `kind()` /
//! `retry_after_seconds()` helpers. Walks every variant so we don't
//! rely on real traffic to exercise them.

use livepeer_open_clearinghouse_sdk::{ErrorKind, OpenClearinghouseError};

#[test]
fn from_code_maps_every_known_code() {
    let cases: &[(&str, ErrorKind)] = &[
        ("INSUFFICIENT_CREDIT", ErrorKind::InsufficientCredit),
        ("SPEND_CAP_EXCEEDED", ErrorKind::SpendCapExceeded),
        ("ACCOUNT_NOT_APPROVED", ErrorKind::AccountNotApproved),
        ("account_not_approved", ErrorKind::AccountNotApproved),
        ("email_not_verified", ErrorKind::EmailNotVerified),
        ("NO_ROUTE_AVAILABLE", ErrorKind::NoRouteAvailable),
        ("rate_limited", ErrorKind::RateLimited),
        ("DUPLICATE_REQUEST", ErrorKind::DuplicateRequest),
        ("DAEMON_UNAVAILABLE", ErrorKind::DaemonUnavailable),
    ];
    for (code, expected) in cases {
        assert_eq!(ErrorKind::from_code(Some(code)), *expected, "code {code:?}");
    }
}

#[test]
fn from_code_falls_back_to_other_for_unknown() {
    assert_eq!(ErrorKind::from_code(Some("WHO_KNOWS")), ErrorKind::Other);
    assert_eq!(ErrorKind::from_code(None), ErrorKind::Other);
}

#[test]
fn kind_on_non_api_returns_other() {
    let err = OpenClearinghouseError::Config("bad".into());
    assert_eq!(err.kind(), ErrorKind::Other);
    assert_eq!(err.retry_after_seconds(), None);
}

#[test]
fn from_response_maps_fastapi_validation_list() {
    let body = serde_json::json!({
        "detail": [{
            "type": "missing",
            "loc": ["body", "settlement", "signature"],
            "msg": "Field required"
        }]
    });
    let err = OpenClearinghouseError::from_response(422, body.clone());
    match err {
        OpenClearinghouseError::Api {
            status,
            code,
            kind,
            message,
            details,
            retry_after_seconds,
        } => {
            assert_eq!(status, 422);
            assert_eq!(code, None);
            assert_eq!(kind, ErrorKind::Other);
            assert_eq!(
                message,
                r#"[{"loc":["body","settlement","signature"],"msg":"Field required","type":"missing"}]"#
            );
            assert_eq!(details, serde_json::json!({ "detail": body["detail"] }));
            assert_eq!(retry_after_seconds, None);
        }
        other => panic!("expected Api, got {other:?}"),
    }
}

#[test]
fn from_response_maps_fastapi_validation_object_and_truncates() {
    let long = "x".repeat(2_000);
    let body = serde_json::json!({ "detail": { "reason": long } });
    let err = OpenClearinghouseError::from_response(422, body.clone());
    match err {
        OpenClearinghouseError::Api {
            code,
            message,
            details,
            ..
        } => {
            assert_eq!(code, None);
            assert_eq!(message.chars().count(), 500);
            assert!(message.starts_with(r#"{"reason":"xxx"#));
            assert_eq!(details, serde_json::json!({ "detail": body["detail"] }));
        }
        other => panic!("expected Api, got {other:?}"),
    }
}

#[test]
fn from_response_keeps_string_detail_and_envelope_behaviour() {
    let err =
        OpenClearinghouseError::from_response(404, serde_json::json!({ "detail": "Not Found" }));
    match err {
        OpenClearinghouseError::Api {
            code,
            message,
            details,
            ..
        } => {
            assert_eq!(code.as_deref(), Some("Not Found"));
            assert_eq!(message, "Not Found");
            assert_eq!(details, serde_json::Value::Null);
        }
        other => panic!("expected Api, got {other:?}"),
    }

    let err = OpenClearinghouseError::from_response(
        422,
        serde_json::json!({
            "error": {
                "code": "settlement_verification_failed",
                "message": "unsigned settlement",
                "details": { "reason": "missing_signature" }
            }
        }),
    );
    match err {
        OpenClearinghouseError::Api {
            code,
            message,
            details,
            ..
        } => {
            assert_eq!(code.as_deref(), Some("settlement_verification_failed"));
            assert_eq!(message, "unsigned settlement");
            assert_eq!(details["reason"], "missing_signature");
        }
        other => panic!("expected Api, got {other:?}"),
    }
}

#[test]
fn from_response_never_panics_on_odd_bodies() {
    for body in [
        serde_json::Value::Null,
        serde_json::json!("HTTP 500"),
        serde_json::json!([]),
        serde_json::json!({ "detail": null }),
        serde_json::json!({ "detail": 42 }),
        serde_json::json!({ "detail": true }),
        serde_json::json!({ "error": "rate_limited" }),
        serde_json::json!({ "error": { "code": 7 } }),
    ] {
        let err = OpenClearinghouseError::from_response(500, body);
        assert!(matches!(
            err,
            OpenClearinghouseError::Api { status: 500, .. }
        ));
    }
}
