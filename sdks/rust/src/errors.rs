use serde_json::Value;
use thiserror::Error;

/// Categorised error class. Matches `Livepeer Open Clearinghouse`'s `error.code` envelope.
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
pub enum OpenClearinghouseError {
    /// Transport / I/O / decode failure.
    #[error("livepeer_open_clearinghouse: transport: {0}")]
    Transport(#[from] reqwest::Error),

    /// `Livepeer Open Clearinghouse` returned an error envelope.
    #[error("livepeer_open_clearinghouse: {message} ({code:?}) [http {status}]")]
    Api {
        status: u16,
        code: Option<String>,
        kind: ErrorKind,
        message: String,
        details: Value,
        retry_after_seconds: Option<u64>,
    },

    /// The broker violated paid-job/v1 and the response cannot be safely settled.
    #[error("livepeer_open_clearinghouse: broker protocol: {message} ({code})")]
    BrokerProtocol {
        code: String,
        message: String,
        status: Option<u16>,
        details: Value,
    },

    /// Configuration mistake at construction time.
    #[error("livepeer_open_clearinghouse: configuration: {0}")]
    Config(String),
}

impl OpenClearinghouseError {
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

    /// Constructor helpers used by the v0.2 handoff-mode client.
    #[must_use]
    pub fn invalid_argument(msg: impl Into<String>) -> Self {
        Self::Config(msg.into())
    }

    #[must_use]
    pub fn transport(msg: impl Into<String>) -> Self {
        Self::Config(format!("transport: {}", msg.into()))
    }

    #[must_use]
    pub fn broker_protocol(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::BrokerProtocol {
            code: code.into(),
            message: message.into(),
            status: None,
            details: Value::Null,
        }
    }

    /// Build an `Api` variant from a JSON body returned by LOC.
    #[must_use]
    pub fn from_response(status: u16, body: Value) -> Self {
        from_response(status, None, Some(body))
    }
}

impl From<serde_json::Error> for OpenClearinghouseError {
    fn from(e: serde_json::Error) -> Self {
        Self::Config(format!("json: {e}"))
    }
}

pub fn from_response(
    status: u16,
    retry_after: Option<u64>,
    body: Option<Value>,
) -> OpenClearinghouseError {
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

    OpenClearinghouseError::Api {
        status,
        kind: ErrorKind::from_code(code.as_deref()),
        code,
        message,
        details,
        retry_after_seconds: retry_after,
    }
}
