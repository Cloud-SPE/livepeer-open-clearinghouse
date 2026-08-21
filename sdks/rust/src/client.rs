//! Async HTTP client for the Livepeer Open Clearinghouse gateway in
//! handoff mode (exec-plan 002).
//!
//! Two flows:
//!
//! - [`Client::submit_job`] — cases (a)/(b)/(c). One-shot mint → broker →
//!   settle composing `POST /v1/jobs` + the broker's `POST /v1/job` +
//!   `POST /v1/jobs/{id}/settle`.
//! - [`Client::open_session`] — case (d). Returns a [`SessionHandle`]
//!   carrying the broker URL + minted envelope; the caller drives the
//!   broker WS/RTMP wire today.

use std::time::Duration;

use base64::Engine as _;
use reqwest::{header, Method};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::errors::OpenClearinghouseError;

// SDK identity sent on every LOC request. Operator-side trust scoring
// keys off this header per the design doc.
pub const SDK_LANG: &str = "rust";
pub const SDK_VERSION: &str = "1.3.3";
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
    pub request_id: String,
    pub work_id: String,
    pub broker_url: String,
    pub protocol: String,
    pub transport: String,
    pub work_unit: String,
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
    pub broker_job_id: String,
    pub protocol: String,
    pub transport: String,
    pub work_unit: String,
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
    pub request_id: String,
    pub work_id: String,
    pub broker_url: String,
    pub protocol: String,
    #[serde(skip)]
    pub capability: String,
    #[serde(skip)]
    pub offering: String,
    pub session: SessionAxes,
    #[serde(skip)]
    pub session_params: Value,
    pub payment_envelope: String,
    pub expected_value_wei: u64,
    pub funded_value_wei: u64,
    pub refill_endpoint: String,
    pub close_endpoint: String,
    pub opened_at: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SessionAxes {
    pub descriptor_schema: String,
    pub attachment: String,
    pub metering: String,
    pub refill: String,
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
    pub transport: Option<&'a str>,
    pub content_type: Option<&'a str>,
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
    pub descriptor_schema: &'a str,
    pub session_params: Value,
    pub estimated_runway_units: u64,
    pub max_total_units: u64,
    #[serde(skip)]
    pub request_id: Option<String>,
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
        let transport = in_.transport.unwrap_or("unary");
        if !matches!(transport, "unary" | "stream" | "multipart") {
            return Err(OpenClearinghouseError::invalid_argument(format!(
                "unsupported job transport {transport}"
            )));
        }
        if transport == "multipart"
            && (!matches!(&in_.body, JobBody::Bytes(_))
                || !in_
                    .content_type
                    .is_some_and(|value| value.starts_with("multipart/form-data")))
        {
            return Err(OpenClearinghouseError::invalid_argument(
                "multipart transport requires a bytes body and multipart/form-data content_type",
            ));
        }

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
            "transport": transport,
            "estimated_units": in_.estimated_units,
            "max_total_units": in_.max_total_units,
        });
        let job: JobOpenResponse = match self
            .request_with_headers(
                Method::POST,
                "/v1/jobs",
                Some(&open_body),
                &[("Idempotency-Key", request_id.as_str())],
            )
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
                        "protocol": job.protocol,
                    })),
                    ..Default::default()
                },
            )
            .await;

        if job.protocol != "paid-job/v1" {
            return Err(OpenClearinghouseError::broker_protocol(
                "protocol_unsupported",
                format!("LOC returned protocol {:?}", job.protocol),
            ));
        }
        if job.transport != transport {
            return Err(OpenClearinghouseError::broker_protocol(
                "protocol_transport_mismatch",
                format!(
                    "LOC returned transport {:?}; requested {transport:?}",
                    job.transport
                ),
            ));
        }

        // 2. Call the broker directly
        let broker_base = job.broker_url.trim_end_matches('/');
        let endpoint = format!("{broker_base}/v1/job");
        let broker = reqwest::Client::new();
        let mut req = broker
            .post(&endpoint)
            .timeout(Duration::from_secs(60))
            .header("Livepeer-Capability", in_.capability)
            .header("Livepeer-Offering", in_.offering)
            .header("Livepeer-Payment", &job.payment_envelope)
            .header("Livepeer-Protocol", &job.protocol)
            .header("Livepeer-Request-Id", &job.request_id);

        if transport == "stream" {
            req = req.header("Accept", "text/event-stream");
        }

        req = match &in_.body {
            JobBody::Json(v) => req
                .header("Content-Type", "application/json")
                .body(serde_json::to_vec(v)?),
            JobBody::Bytes(b) => {
                let content_type = in_.content_type.unwrap_or("application/octet-stream");
                req.header("Content-Type", content_type).body(b.to_vec())
            }
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
        let initial_job_id = res
            .headers()
            .get("livepeer-job-id")
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let response_headers = res.headers().clone();
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

        // reqwest does not expose HTTP response trailers. Stream claims and
        // signed settlement are retained by the broker under Livepeer-Job-Id
        // and retrieved through an ordinary response instead.
        let claim_headers = if transport == "stream" {
            let broker_job_id = initial_job_id.ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "stream response missing Livepeer-Job-Id",
                )
            })?;
            let settlement_url =
                format!("{broker_base}/v1/settlement/{}", urlencode(&broker_job_id));
            let mut terminal = None;
            for attempt in 0..4 {
                let query = broker.get(&settlement_url).send().await.map_err(|e| {
                    OpenClearinghouseError::transport(format!("settlement query: {e}"))
                })?;
                if query.status().as_u16() == 202 && attempt < 3 {
                    tokio::time::sleep(Duration::from_millis(50 * 2_u64.pow(attempt))).await;
                    continue;
                }
                terminal = Some(query);
                break;
            }
            let terminal = terminal.ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "settlement query did not reach a terminal response",
                )
            })?;
            let queried_job_id = terminal
                .headers()
                .get("livepeer-job-id")
                .and_then(|v| v.to_str().ok());
            if queried_job_id != Some(broker_job_id.as_str()) {
                return Err(OpenClearinghouseError::broker_protocol(
                    "broker_job_id_mismatch",
                    "settlement query returned a different job id",
                ));
            }
            terminal.headers().clone()
        } else {
            response_headers
        };

        let work_units = claim_headers
            .get("livepeer-work-units")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "terminal response missing Livepeer-Work-Units",
                )
            })?;
        let actual_units = work_units.parse::<u64>().map_err(|_| {
            OpenClearinghouseError::broker_protocol(
                "broker_protocol_error",
                "invalid Livepeer-Work-Units",
            )
        })?;
        let broker_work_unit = claim_headers
            .get("livepeer-work-unit")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "terminal response missing Livepeer-Work-Unit",
                )
            })?
            .to_string();
        if broker_work_unit != job.work_unit {
            return Err(OpenClearinghouseError::broker_protocol(
                "work_unit_mismatch",
                format!(
                    "broker reported work unit {broker_work_unit:?}; expected {:?}",
                    job.work_unit
                ),
            ));
        }
        let broker_job_id = claim_headers
            .get("livepeer-job-id")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "terminal response missing Livepeer-Job-Id",
                )
            })?
            .to_string();

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
        let mut settle_body = serde_json::json!({
            "actual_units": actual_units,
            "broker_job_id": broker_job_id,
            "work_unit": broker_work_unit,
        });
        let encoded = claim_headers
            .get("livepeer-settlement")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "terminal response missing Livepeer-Settlement",
                )
            })?;
        let raw = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|_| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "terminal response has malformed Livepeer-Settlement",
                )
            })?;
        let settlement = serde_json::from_slice::<Value>(&raw).map_err(|_| {
            OpenClearinghouseError::broker_protocol(
                "broker_protocol_error",
                "terminal response has malformed Livepeer-Settlement",
            )
        })?;
        settle_body["settlement"] = settlement;
        let settled: JobSettleResponse = match self
            .request_with_retry(Method::POST, &job.settle_endpoint, Some(&settle_body), 3)
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
                        "protocol": job.protocol,
                        "transport": job.transport,
                        "work_unit": job.work_unit,
                        "broker_job_id": broker_job_id,
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
            broker_job_id,
            protocol: job.protocol,
            transport: job.transport,
            work_unit: broker_work_unit,
            actual_units: settled.actual_units,
            billed_value_wei: settled.billed_value_wei,
            refund_wei: settled.refund_wei,
            outcome: settled.outcome,
            cap_status: settled.cap_status,
            request_id: job.request_id,
        })
    }

    // ---- sessions (case d) ----

    /// Open a long-running session and return a [`SessionHandle`].
    ///
    /// `max_total_units` is a hard spend ceiling. Whether the session can
    /// extend within that ceiling comes from the offering's
    /// `session.refill` axis: `bounded` drains without refilling, while
    /// `extensible` uses the broker's authoritative HTTP top-up contract.
    ///
    /// `estimated_runway_units` is the initial chunk LOC mints
    /// toward; [`SessionRunner`] tops up automatically as the broker
    /// reports a normative low balance.
    pub async fn open_session(
        &self,
        in_: OpenSessionInput<'_>,
    ) -> Result<SessionHandle, OpenClearinghouseError> {
        self.emit_sdk_init_once().await;
        let request_id = in_
            .request_id
            .clone()
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let mut handle: SessionHandle = self
            .request_with_headers(
                Method::POST,
                "/v1/sessions",
                Some(&in_),
                &[("Idempotency-Key", request_id.as_str())],
            )
            .await?;
        if handle.protocol != "paid-session/v1" {
            return Err(OpenClearinghouseError::broker_protocol(
                "protocol_unsupported",
                format!("unsupported session protocol {}", handle.protocol),
            ));
        }
        if handle.session.descriptor_schema != in_.descriptor_schema {
            return Err(OpenClearinghouseError::broker_protocol(
                "descriptor_schema_mismatch",
                "session descriptor schema mismatch",
            ));
        }
        handle.capability = in_.capability.to_string();
        handle.offering = in_.offering.to_string();
        handle.session_params = in_.session_params.clone();
        self.telemetry
            .emit(
                "session.opened",
                crate::telemetry::EmitOptions {
                    correlation_id: Some(handle.session_id.clone()),
                    payload: Some(serde_json::json!({
                        "capability": in_.capability,
                        "offering": in_.offering,
                        "protocol": handle.protocol,
                        "descriptor_schema": handle.session.descriptor_schema,
                        "refill": handle.session.refill,
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
        request_id: Option<&str>,
        rebind_from: Option<&str>,
        replaces_request_id: Option<&str>,
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
        let mut body = serde_json::json!({
            "observed_consumed_units": observed_consumed_units,
        });
        if let Some(rebind_from) = rebind_from {
            body["rebind_from"] = Value::String(rebind_from.to_string());
            body["replaces_request_id"] =
                replaces_request_id.map_or(Value::Null, |value| Value::String(value.to_string()));
        }
        let idempotency_key =
            request_id.map_or_else(|| uuid::Uuid::new_v4().to_string(), ToString::to_string);
        let result: Value = match self
            .request_with_headers(
                Method::POST,
                &format!("/v1/sessions/{session_id}/refill"),
                Some(&body),
                &[("Idempotency-Key", idempotency_key.as_str())],
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
        settlement: Value,
    ) -> Result<Value, OpenClearinghouseError> {
        let mut body = serde_json::json!({ "actual_units": actual_units });
        if let Some(o) = outcome {
            body["outcome"] = Value::String(o.to_string());
        }
        body["settlement"] = settlement;
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

    /// Wrapper around :meth:`request` that retries transient failures
    /// (5xx, 429, transport errors) with exponential backoff. 4xx
    /// surfaces immediately. Used by the settle path.
    async fn request_with_retry<B: Serialize + Sync, R: for<'de> Deserialize<'de>>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
        max_retries: u32,
    ) -> Result<R, OpenClearinghouseError> {
        let mut backoff = std::time::Duration::from_millis(500);
        let mut last_err: Option<OpenClearinghouseError> = None;
        for attempt in 1..=max_retries.max(1) {
            match self.request(method.clone(), path, body).await {
                Ok(value) => return Ok(value),
                Err(e) => {
                    if let OpenClearinghouseError::Api { status, .. } = &e {
                        if *status < 500 && *status != 429 {
                            return Err(e);
                        }
                    }
                    last_err = Some(e);
                    if attempt >= max_retries {
                        break;
                    }
                    tokio::time::sleep(backoff).await;
                    backoff *= 2;
                }
            }
        }
        Err(last_err.unwrap_or_else(|| {
            OpenClearinghouseError::transport("request_with_retry exhausted attempts")
        }))
    }

    async fn request<B: Serialize + Sync, R: for<'de> Deserialize<'de>>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
    ) -> Result<R, OpenClearinghouseError> {
        self.request_with_headers(method, path, body, &[]).await
    }

    async fn request_with_headers<B: Serialize + Sync, R: for<'de> Deserialize<'de>>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
        headers: &[(&str, &str)],
    ) -> Result<R, OpenClearinghouseError> {
        let url = format!("{}{}", self.base_url, path);
        let mut req = self.http.request(method, &url);
        for (name, value) in headers {
            req = req.header(*name, *value);
        }
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
