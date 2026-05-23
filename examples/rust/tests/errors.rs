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
