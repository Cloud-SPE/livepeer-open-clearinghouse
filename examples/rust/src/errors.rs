use serde_json::Value;
use thiserror::Error;

/// Categorised error class. Matches `PymtHouse`'s `error.code` envelope.
/// Use `ErrorKind::from_code` to map a code string in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    InsufficientCredit,
    SpendCapExceeded,
    AccountNotApproved,
    EmailNotVerified,
    NoRouteAvailable,
    RateLimited,
    DuplicateRequest,
    DaemonUnavailable,
    Other,
}

impl ErrorKind {
    #[must_use]
    pub fn from_code(code: Option<&str>) -> Self {
        match code {
            Some("INSUFFICIENT_CREDIT") => Self::InsufficientCredit,
            Some("SPEND_CAP_EXCEEDED") => Self::SpendCapExceeded,
            Some("ACCOUNT_NOT_APPROVED" | "account_not_approved") => Self::AccountNotApproved,
            Some("email_not_verified") => Self::EmailNotVerified,
            Some("NO_ROUTE_AVAILABLE") => Self::NoRouteAvailable,
            Some("rate_limited") => Self::RateLimited,
            Some("DUPLICATE_REQUEST") => Self::DuplicateRequest,
            Some("DAEMON_UNAVAILABLE") => Self::DaemonUnavailable,
            _ => Self::Other,
        }
    }
}

/// The single error type the SDK returns. Wraps wire-level (`Api`) and
/// transport-level (`Transport`) failures so callers can match on
/// `kind()` for common cases without losing the full context.
#[derive(Debug, Error)]
pub enum PymtHouseError {
    /// Transport / I/O / decode failure.
    #[error("pymthouse: transport: {0}")]
    Transport(#[from] reqwest::Error),

    /// `PymtHouse` returned an error envelope.
    #[error("pymthouse: {message} ({code:?}) [http {status}]")]
    Api {
        status: u16,
        code: Option<String>,
        kind: ErrorKind,
        message: String,
        details: Value,
        retry_after_seconds: Option<u64>,
    },

    /// Configuration mistake at construction time.
    #[error("pymthouse: configuration: {0}")]
    Config(String),
}

impl PymtHouseError {
    #[must_use]
    pub const fn kind(&self) -> ErrorKind {
        match self {
            Self::Api { kind, .. } => *kind,
            _ => ErrorKind::Other,
        }
    }

    #[must_use]
    pub const fn retry_after_seconds(&self) -> Option<u64> {
        match self {
            Self::Api {
                retry_after_seconds,
                ..
            } => *retry_after_seconds,
            _ => None,
        }
    }
}

pub fn from_response(status: u16, retry_after: Option<u64>, body: Option<Value>) -> PymtHouseError {
    let body = body.unwrap_or(Value::Null);
    let envelope = body.get("error");

    let code = envelope
        .and_then(|e| e.get("code"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            body.get("detail")
                .and_then(Value::as_str)
                .map(str::to_string)
        });

    let message = envelope
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            body.get("detail")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_else(|| format!("HTTP {status}"));

    let details = envelope
        .and_then(|e| e.get("details"))
        .cloned()
        .unwrap_or(Value::Null);

    PymtHouseError::Api {
        status,
        kind: ErrorKind::from_code(code.as_deref()),
        code,
        message,
        details,
        retry_after_seconds: retry_after,
    }
}
