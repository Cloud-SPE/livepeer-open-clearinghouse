//! Async HTTP client for the Livepeer Open Clearinghouse gateway in
//! handoff mode (exec-plan 002).
//!
//! Two flows:
//!
//! - [`Client::submit_job`] — cases (a)/(b)/(c). One-shot mint → broker →
//!   settle composing `POST /v1/jobs` + the broker's `POST /v1/cap` +
//!   `POST /v1/jobs/{id}/settle`.
//! - [`Client::open_session`] — case (d). Returns a [`SessionHandle`]
//!   carrying the broker URL + minted envelope; the caller drives the
//!   broker WS/RTMP wire today.

use std::time::Duration;

use reqwest::{header, Method};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::errors::OpenClearinghouseError;

// SDK identity sent on every LOC request. Operator-side trust scoring
// keys off this header per the design doc.
pub const SDK_LANG: &str = "rust";
pub const SDK_VERSION: &str = "0.2.0";
pub const SDK_GIT_SHA: &str = "dev";

fn default_sdk_identity() -> String {
    format!("{SDK_LANG}/{SDK_VERSION}/{SDK_GIT_SHA}")
}

#[derive(Debug, Clone, Deserialize)]
pub struct Offering {
    pub id: String,
    pub price_per_work_unit_wei: String,
    pub work_unit: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Capability {
    pub name: String,
    pub work_unit: Option<String>,
    pub offerings: Vec<Offering>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Orchestrator {
    pub eth_address: String,
    pub worker_url: String,
    pub capabilities: Vec<Capability>,
    pub signature_status: String,
    pub freshness_status: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct CapStatus {
    pub session_pct_used: f64,
    pub spend_period_pct_used: Option<f64>,
    pub user_balance_pct_used: Option<f64>,
    pub operator_pool_pct_used: Option<f64>,
    pub will_refuse_next_refill: bool,
    pub winddown_reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JobOpenResponse {
    pub job_id: String,
    pub work_id: String,
    pub broker_url: String,
    pub mode: String,
    pub payment_envelope: String,
    pub expected_value_wei: u64,
    pub funded_value_wei: u64,
    pub settle_endpoint: String,
    pub opened_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JobSettleResponse {
    pub job_id: String,
    pub work_id: String,
    pub actual_units: u64,
    pub billed_value_wei: u64,
    pub refund_wei: u64,
    pub outcome: String,
    pub closed_at: String,
    pub cap_status: CapStatus,
}

/// End-to-end return of [`Client::submit_job`].
#[derive(Debug, Clone)]
pub struct JobResult {
    pub body: Option<Value>,
    pub body_text: String,
    pub status: u16,
    pub job_id: String,
    pub work_id: String,
    pub actual_units: u64,
    pub billed_value_wei: u64,
    pub refund_wei: u64,
    pub outcome: String,
    pub cap_status: CapStatus,
    pub request_id: String,
}

/// Returned from [`Client::open_session`] (case d).
#[derive(Debug, Clone, Deserialize)]
pub struct SessionHandle {
    pub session_id: String,
    pub work_id: String,
    pub broker_url: String,
    pub mode: String,
    pub payment_envelope: String,
    pub expected_value_wei: u64,
    pub funded_value_wei: u64,
    pub refill_endpoint: String,
    pub close_endpoint: String,
    pub opened_at: String,
}

#[derive(Debug, Clone)]
pub struct ClientOptions {
    pub base_url: String,
    pub api_key: String,
    pub timeout: Duration,
    pub sdk_identity: String,
}

impl ClientOptions {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            api_key: api_key.into(),
            timeout: Duration::from_secs(15),
            sdk_identity: default_sdk_identity(),
        }
    }
}

pub struct Client {
    base_url: String,
    http: reqwest::Client,
    telemetry: std::sync::Arc<crate::telemetry::TelemetryEmitter>,
    init_emitted: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl Clone for Client {
    fn clone(&self) -> Self {
        Self {
            base_url: self.base_url.clone(),
            http: self.http.clone(),
            telemetry: self.telemetry.clone(),
            init_emitted: self.init_emitted.clone(),
        }
    }
}

impl std::fmt::Debug for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Client")
            .field("base_url", &self.base_url)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SubmitJobInput<'a> {
    pub capability: &'a str,
    pub offering: &'a str,
    pub estimated_units: u64,
    /// JSON body (will be serialized) or raw bytes.
    pub body: JobBody<'a>,
    /// Optional worst-case ceiling. None defaults to `estimated_units`.
    pub max_total_units: Option<u64>,
    pub request_id: Option<String>,
    pub spec_version: Option<&'a str>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(untagged)]
pub enum JobBody<'a> {
    Json(Value),
    Bytes(#[serde(skip)] &'a [u8]),
}

#[derive(Debug, Clone, Serialize)]
pub struct OpenSessionInput<'a> {
    pub capability: &'a str,
    pub offering: &'a str,
    pub estimated_runway_units: u64,
    pub max_total_units: u64,
}

impl Client {
    pub fn new(opts: ClientOptions) -> Result<Self, OpenClearinghouseError> {
        if !opts.api_key.starts_with("pymth_") {
            return Err(OpenClearinghouseError::invalid_argument(
                "api_key looks wrong (expected to start with pymth_)",
            ));
        }
        let mut headers = header::HeaderMap::new();
        headers.insert(
            "X-API-Key",
            header::HeaderValue::from_str(&opts.api_key).map_err(|e| {
                OpenClearinghouseError::invalid_argument(format!("api_key not valid header: {e}"))
            })?,
        );
        headers.insert(
            "Livepeer-Open-Clearinghouse-SDK",
            header::HeaderValue::from_str(&opts.sdk_identity).map_err(|e| {
                OpenClearinghouseError::invalid_argument(format!("sdk_identity invalid: {e}"))
            })?,
        );
        let http = reqwest::Client::builder()
            .timeout(opts.timeout)
            .default_headers(headers)
            .build()
            .map_err(|e| OpenClearinghouseError::transport(format!("build http client: {e}")))?;
        let base_url = opts.base_url.trim_end_matches('/').to_string();
        let telemetry = crate::telemetry::TelemetryEmitter::new(
            http.clone(),
            &base_url,
            opts.api_key.clone(),
            opts.sdk_identity.clone(),
            crate::telemetry::EmitterConfig::default(),
        );
        Ok(Self {
            init_emitted: std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            telemetry,
            base_url,
            http,
        })
    }

    /// Public access for advanced callers; most users never touch this.
    #[must_use]
    pub fn telemetry(&self) -> &std::sync::Arc<crate::telemetry::TelemetryEmitter> {
        &self.telemetry
    }

    /// Drain the telemetry buffer with one last flush. Idempotent.
    pub async fn close(&self) {
        self.telemetry.close().await;
    }

    async fn emit_sdk_init_once(&self) {
        if !self
            .init_emitted
            .swap(true, std::sync::atomic::Ordering::Relaxed)
        {
            self.telemetry
                .emit(
                    "sdk.init",
                    crate::telemetry::EmitOptions {
                        payload: Some(serde_json::json!({
                            "lang": SDK_LANG,
                            "semver": SDK_VERSION,
                            "git_sha7": SDK_GIT_SHA,
                            "runtime_version": format!("rust/{}", env!("CARGO_PKG_VERSION")),
                        })),
                        ..Default::default()
                    },
                )
                .await;
        }
    }

    // ---- discovery ----

    pub async fn list_capabilities(&self) -> Result<Vec<Capability>, OpenClearinghouseError> {
        let val: Value = self
            .request(Method::GET, "/v1/capabilities", None::<&Value>)
            .await?;
        let items = val
            .get("items")
            .cloned()
            .ok_or_else(|| OpenClearinghouseError::transport("missing items"))?;
        Ok(serde_json::from_value(items)?)
    }

    pub async fn list_orchestrators(
        &self,
        capability: Option<&str>,
    ) -> Result<Vec<Orchestrator>, OpenClearinghouseError> {
        let path = capability.map_or_else(
            || "/v1/orchestrators".to_string(),
            |c| format!("/v1/orchestrators?capability={}", urlencode(c)),
        );
        let val: Value = self.request(Method::GET, &path, None::<&Value>).await?;
        let items = val
            .get("items")
            .cloned()
            .ok_or_else(|| OpenClearinghouseError::transport("missing items"))?;
        Ok(serde_json::from_value(items)?)
    }

    // ---- jobs (cases a/b/c) ----

    pub async fn submit_job(
        &self,
        in_: SubmitJobInput<'_>,
    ) -> Result<JobResult, OpenClearinghouseError> {
        let request_id = in_
            .request_id
            .clone()
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let spec_version = in_.spec_version.unwrap_or("0.1");

        self.emit_sdk_init_once().await;
        self.telemetry
            .emit(
                "request.mint_started",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(request_id.clone()),
                    payload: Some(serde_json::json!({
                        "capability": in_.capability,
                        "offering": in_.offering,
                        "estimated_units": in_.estimated_units,
                    })),
                    ..Default::default()
                },
            )
            .await;
        let mint_started = std::time::Instant::now();

        // 1. Open the job
        let open_body = serde_json::json!({
            "capability": in_.capability,
            "offering": in_.offering,
            "estimated_units": in_.estimated_units,
            "max_total_units": in_.max_total_units,
        });
        let job: JobOpenResponse = match self
            .request(Method::POST, "/v1/jobs", Some(&open_body))
            .await
        {
            Ok(j) => j,
            Err(e) => {
                self.telemetry
                    .emit(
                        "request.error",
                        crate::telemetry::EmitOptions {
                            correlation_id: Some(request_id.clone()),
                            payload: Some(serde_json::json!({
                                "phase": "mint",
                                "error_code": telemetry_error_code(&e),
                            })),
                            ..Default::default()
                        },
                    )
                    .await;
                return Err(e);
            }
        };
        self.telemetry
            .emit(
                "request.mint_completed",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(request_id.clone()),
                    #[allow(clippy::cast_possible_truncation)]
                    payload: Some(serde_json::json!({
                        "latency_ms": mint_started.elapsed().as_millis() as u64,
                        "funded_value_wei": job.funded_value_wei,
                        "mode": job.mode,
                    })),
                    ..Default::default()
                },
            )
            .await;

        // 2. Call the broker directly
        let endpoint = format!("{}/v1/cap", job.broker_url.trim_end_matches('/'));
        let mut req = reqwest::Client::new()
            .post(&endpoint)
            .timeout(Duration::from_secs(60))
            .header("Livepeer-Capability", in_.capability)
            .header("Livepeer-Offering", in_.offering)
            .header("Livepeer-Payment", &job.payment_envelope)
            .header("Livepeer-Mode", &job.mode)
            .header("Livepeer-Spec-Version", spec_version)
            .header("Livepeer-Request-Id", &request_id);

        req = match &in_.body {
            JobBody::Json(v) => req
                .header("Content-Type", "application/json")
                .body(serde_json::to_vec(v)?),
            JobBody::Bytes(b) => req
                .header("Content-Type", "application/octet-stream")
                .body(b.to_vec()),
        };

        let res = req
            .send()
            .await
            .map_err(|e| OpenClearinghouseError::transport(format!("broker call: {e}")))?;
        let status = res.status().as_u16();
        let ctype = res
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        let actual_units = res
            .headers()
            .get("livepeer-work-units")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(0);
        let body_bytes = res
            .bytes()
            .await
            .map_err(|e| OpenClearinghouseError::transport(format!("read broker body: {e}")))?;
        let body_text = String::from_utf8_lossy(&body_bytes).to_string();
        let body: Option<Value> = if ctype.contains("json") && !body_bytes.is_empty() {
            serde_json::from_slice(&body_bytes).ok()
        } else {
            None
        };

        // 3. Settle
        self.telemetry
            .emit(
                "request.settle_started",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(request_id.clone()),
                    ..Default::default()
                },
            )
            .await;
        let settle_started = std::time::Instant::now();
        let settle_body = serde_json::json!({ "actual_units": actual_units });
        let settled: JobSettleResponse = match self
            .request(Method::POST, &job.settle_endpoint, Some(&settle_body))
            .await
        {
            Ok(s) => s,
            Err(e) => {
                self.telemetry
                    .emit(
                        "request.error",
                        crate::telemetry::EmitOptions {
                            correlation_id: Some(request_id.clone()),
                            payload: Some(serde_json::json!({
                                "phase": "settle",
                                "error_code": telemetry_error_code(&e),
                            })),
                            ..Default::default()
                        },
                    )
                    .await;
                return Err(e);
            }
        };
        #[allow(clippy::cast_possible_truncation)]
        let settle_latency_ms = settle_started.elapsed().as_millis() as u64;
        self.telemetry
            .emit(
                "request.settle_completed",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(request_id.clone()),
                    payload: Some(serde_json::json!({
                        "latency_ms": settle_latency_ms,
                        "refund_wei": settled.refund_wei,
                        "billed_value_wei": settled.billed_value_wei,
                        "outcome": settled.outcome,
                    })),
                    ..Default::default()
                },
            )
            .await;
        self.telemetry
            .emit(
                "request.completed",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(request_id.clone()),
                    payload: Some(serde_json::json!({
                        "capability": in_.capability,
                        "offering": in_.offering,
                        "mode": job.mode,
                        "estimated_units": in_.estimated_units,
                        "actual_units": settled.actual_units,
                        "billed_value_wei": settled.billed_value_wei,
                        "refund_wei": settled.refund_wei,
                        "outcome": settled.outcome,
                        "broker_url": job.broker_url,
                    })),
                    ..Default::default()
                },
            )
            .await;

        Ok(JobResult {
            body,
            body_text,
            status,
            job_id: settled.job_id,
            work_id: settled.work_id,
            actual_units: settled.actual_units,
            billed_value_wei: settled.billed_value_wei,
            refund_wei: settled.refund_wei,
            outcome: settled.outcome,
            cap_status: settled.cap_status,
            request_id,
        })
    }

    // ---- sessions (case d) ----

    pub async fn open_session(
        &self,
        in_: OpenSessionInput<'_>,
    ) -> Result<SessionHandle, OpenClearinghouseError> {
        self.emit_sdk_init_once().await;
        let handle: SessionHandle = self
            .request(Method::POST, "/v1/sessions", Some(&in_))
            .await?;
        self.telemetry
            .emit(
                "session.opened",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(handle.session_id.clone()),
                    payload: Some(serde_json::json!({
                        "capability": in_.capability,
                        "offering": in_.offering,
                        "mode": handle.mode,
                        "max_total_units": in_.max_total_units,
                        "initial_runway_units": in_.estimated_runway_units,
                    })),
                    ..Default::default()
                },
            )
            .await;
        Ok(handle)
    }

    pub async fn refill_session(
        &self,
        session_id: &str,
        observed_consumed_units: Option<u64>,
    ) -> Result<Value, OpenClearinghouseError> {
        self.telemetry
            .emit(
                "session.refill_requested",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(session_id.to_string()),
                    ..Default::default()
                },
            )
            .await;
        let started = std::time::Instant::now();
        let body = serde_json::json!({
            "observed_consumed_units": observed_consumed_units,
        });
        let result: Value = match self
            .request(
                Method::POST,
                &format!("/v1/sessions/{session_id}/refill"),
                Some(&body),
            )
            .await
        {
            Ok(v) => v,
            Err(e) => {
                if let OpenClearinghouseError::Api {
                    status, details, ..
                } = &e
                {
                    if *status == 402 {
                        self.telemetry
                            .emit(
                                "session.refill_denied",
                                crate::telemetry::EmitOptions {
                                    correlation_id: Some(session_id.to_string()),
                                    payload: Some(serde_json::json!({
                                        "which": details.get("which"),
                                        "remaining_wei": details.get("remaining_wei"),
                                    })),
                                    ..Default::default()
                                },
                            )
                            .await;
                    } else {
                        self.telemetry
                            .emit(
                                "session.error",
                                crate::telemetry::EmitOptions {
                                    correlation_id: Some(session_id.to_string()),
                                    payload: Some(serde_json::json!({
                                        "phase": "refill",
                                        "error_code": telemetry_error_code(&e),
                                    })),
                                    ..Default::default()
                                },
                            )
                            .await;
                    }
                }
                return Err(e);
            }
        };
        #[allow(clippy::cast_possible_truncation)]
        let latency_ms = started.elapsed().as_millis() as u64;
        self.telemetry
            .emit(
                "session.refill_granted",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(session_id.to_string()),
                    payload: Some(serde_json::json!({
                        "latency_ms": latency_ms,
                        "refill_seq": result.get("refill_seq"),
                        "funded_value_wei": result.get("funded_value_wei"),
                        "cap_status": result.get("cap_status"),
                    })),
                    ..Default::default()
                },
            )
            .await;
        Ok(result)
    }

    pub async fn close_session(
        &self,
        session_id: &str,
        actual_units: u64,
        outcome: Option<&str>,
        settlement: Option<Value>,
    ) -> Result<Value, OpenClearinghouseError> {
        let mut body = serde_json::json!({ "actual_units": actual_units });
        if let Some(o) = outcome {
            body["outcome"] = Value::String(o.to_string());
        }
        if let Some(s) = settlement {
            body["settlement"] = s;
        }
        let result: Value = match self
            .request(
                Method::POST,
                &format!("/v1/sessions/{session_id}/close"),
                Some(&body),
            )
            .await
        {
            Ok(v) => v,
            Err(e) => {
                self.telemetry
                    .emit(
                        "session.error",
                        crate::telemetry::EmitOptions {
                            correlation_id: Some(session_id.to_string()),
                            payload: Some(serde_json::json!({
                                "phase": "close",
                                "error_code": telemetry_error_code(&e),
                            })),
                            ..Default::default()
                        },
                    )
                    .await;
                return Err(e);
            }
        };
        self.telemetry
            .emit(
                "session.closed",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(session_id.to_string()),
                    payload: Some(serde_json::json!({
                        "actual_units": result.get("actual_units"),
                        "billed_value_wei": result.get("billed_value_wei"),
                        "refund_wei": result.get("refund_wei"),
                        "outcome": result.get("outcome"),
                        "closed_by": "customer",
                    })),
                    ..Default::default()
                },
            )
            .await;
        Ok(result)
    }

    pub async fn get_session_status(
        &self,
        session_id: &str,
    ) -> Result<Value, OpenClearinghouseError> {
        self.request(
            Method::GET,
            &format!("/v1/sessions/{session_id}"),
            None::<&Value>,
        )
        .await
    }

    // ---- internals ----

    async fn request<B: Serialize + Sync, R: for<'de> Deserialize<'de>>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
    ) -> Result<R, OpenClearinghouseError> {
        let url = format!("{}{}", self.base_url, path);
        let mut req = self.http.request(method, &url);
        if let Some(b) = body {
            req = req.json(b);
        }
        let res = req
            .send()
            .await
            .map_err(|e| OpenClearinghouseError::transport(format!("send: {e}")))?;
        let status = res.status();
        let bytes = res
            .bytes()
            .await
            .map_err(|e| OpenClearinghouseError::transport(format!("read body: {e}")))?;
        if status.is_success() {
            if bytes.is_empty() {
                // Some endpoints return no body
                return serde_json::from_value(serde_json::Value::Null).map_err(Into::into);
            }
            return serde_json::from_slice(&bytes).map_err(Into::into);
        }
        let body_value: Value = serde_json::from_slice(&bytes)
            .unwrap_or_else(|_| Value::String(format!("HTTP {}", status.as_u16())));
        Err(OpenClearinghouseError::from_response(
            status.as_u16(),
            body_value,
        ))
    }
}

fn telemetry_error_code(err: &OpenClearinghouseError) -> Option<String> {
    match err {
        OpenClearinghouseError::Api { code, .. } => code.clone(),
        _ => None,
    }
}

fn urlencode(s: &str) -> String {
    // Minimal percent-encoding for the capability filter — avoids
    // pulling in `urlencoding` as a dep for one call site.
    use std::fmt::Write as _;
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => {
                let _ = write!(out, "%{b:02X}");
            }
        }
    }
    out
}
