use std::time::Duration;

use reqwest::{header, Method};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::errors::{from_response, OpenClearinghouseError};

#[derive(Debug, Clone, Deserialize)]
pub struct MintResponse {
    pub payment_id: String,
    pub work_id: String,
    pub payment_bytes: String,
    pub expected_value_wei: String,
    pub funded_value_wei: String,
    pub recipient_eth_address: String,
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
pub struct UsageRecordView {
    pub id: String,
    pub actual_work_units: u64,
    pub final_charge_wei: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct UsageReportResult {
    pub refunded_wei: String,
    pub payment_status: String,
    pub new_balance_wei: String,
    pub usage: UsageRecordView,
}

#[derive(Debug, Clone)]
pub struct ClientOptions {
    pub base_url: String,
    pub api_key: String,
    pub timeout: Duration,
}

impl ClientOptions {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            api_key: api_key.into(),
            timeout: Duration::from_secs(15),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Client {
    base_url: String,
    http: reqwest::Client,
}

#[derive(Debug, Serialize)]
pub struct MintPaymentInput<'a> {
    pub capability: &'a str,
    pub offering: &'a str,
    pub work_units: u64,
    #[serde(skip)]
    pub idempotency_key: Option<&'a str>,
}

/// Body shape for [`Client::submit_job`].
#[derive(Debug, Clone)]
pub enum JobBody<'a> {
    /// Serialised as JSON; `Content-Type: application/json` is set automatically.
    Json(Value),
    /// Sent raw. Pass `content_type` on the input or it defaults to `application/octet-stream`.
    Bytes(&'a [u8]),
}

#[derive(Debug, Clone, Deserialize)]
pub struct RouteView {
    pub eth_address: String,
    pub worker_url: String,
    pub capability: String,
    pub offering: String,
    pub price_per_work_unit_wei: String,
}

#[derive(Debug, Clone)]
pub struct JobResult {
    /// JSON body if the response was JSON; `Value::String` containing the raw text otherwise.
    pub body: Value,
    pub status: u16,
    pub payment_id: String,
    pub recipient_eth_address: String,
    pub request_id: String,
    pub raw_headers: Vec<(String, String)>,
}

/// Input for [`Client::submit_job`]. Sensible defaults: 60s timeout,
/// generated UUID `request_id`, `http-reqresp@v0` mode, `0.1` spec.
#[derive(Debug)]
pub struct SubmitJobInput<'a> {
    pub capability: &'a str,
    pub offering: &'a str,
    pub work_units: u64,
    pub body: JobBody<'a>,
    pub content_type: Option<&'a str>,
    pub idempotency_key: Option<&'a str>,
    pub request_id: Option<&'a str>,
    pub mode: Option<&'a str>,
    pub spec_version: Option<&'a str>,
    pub timeout: Option<Duration>,
}

#[derive(Debug, Serialize)]
pub struct ReportUsageInput<'a> {
    pub payment_id: &'a str,
    pub actual_work_units: u64,
    #[serde(skip)]
    pub idempotency_key: Option<&'a str>,
}

#[derive(Deserialize)]
struct Wrapped<T> {
    items: Vec<T>,
}

/// Internal: the outcome of one `attempt_once` inside `submit_job`.
/// Factored out so clippy's `type_complexity` lint stays happy.
type AttemptOutcome = (MintResponse, u16, Vec<(String, String)>, Vec<u8>);

impl Client {
    pub fn new(opts: ClientOptions) -> Result<Self, OpenClearinghouseError> {
        if !opts.api_key.starts_with("pymth_") {
            return Err(OpenClearinghouseError::Config(
                "api_key looks wrong (expected to start with pymth_)".into(),
            ));
        }
        let mut headers = header::HeaderMap::new();
        headers.insert(
            header::HeaderName::from_static("x-api-key"),
            header::HeaderValue::from_str(&opts.api_key)
                .map_err(|e| OpenClearinghouseError::Config(e.to_string()))?,
        );
        headers.insert(
            header::ACCEPT,
            header::HeaderValue::from_static("application/json"),
        );
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .timeout(opts.timeout)
            .build()?;
        Ok(Self {
            base_url: opts.base_url.trim_end_matches('/').to_string(),
            http,
        })
    }

    // ---- discovery ----

    pub async fn list_capabilities(&self) -> Result<Vec<Capability>, OpenClearinghouseError> {
        let resp: Wrapped<Capability> = self
            .send(Method::GET, "/v1/capabilities", None, None)
            .await?;
        Ok(resp.items)
    }

    pub async fn list_orchestrators(
        &self,
        capability: Option<&str>,
    ) -> Result<Vec<Orchestrator>, OpenClearinghouseError> {
        let path = capability.map_or_else(
            || "/v1/orchestrators".to_string(),
            |c| format!("/v1/orchestrators?capability={}", urlencoded(c)),
        );
        let resp: Wrapped<Orchestrator> = self.send(Method::GET, &path, None, None).await?;
        Ok(resp.items)
    }

    // ---- payments ----

    pub async fn mint_payment(
        &self,
        input: MintPaymentInput<'_>,
    ) -> Result<MintResponse, OpenClearinghouseError> {
        let body = serde_json::json!({
            "capability": input.capability,
            "offering": input.offering,
            "work_units": input.work_units,
        });
        self.send(
            Method::POST,
            "/v1/payments/mint",
            Some(body),
            input.idempotency_key,
        )
        .await
    }

    /// Mint a payment, route to an orchestrator, return its response.
    ///
    /// The load-bearing convenience method: route selection + payment
    /// mint + orch HTTP call with the canonical `POST <broker>/v1/cap`
    /// shape and the five Livepeer headers.
    ///
    /// **Don't put a `model` field in OpenAI-shaped bodies** — the orch
    /// routes via `Livepeer-Offering` and most upstreams (vLLM, etc.)
    /// will 404 on a mismatched model name. The offering identifies the
    /// model.
    pub async fn submit_job(
        &self,
        input: SubmitJobInput<'_>,
    ) -> Result<JobResult, OpenClearinghouseError> {
        // 1. Route.
        let path = format!(
            "/v1/routes?capability={}&offering={}",
            urlencoded(input.capability),
            urlencoded(input.offering),
        );
        let route: RouteView = self.send(Method::GET, &path, None, None).await?;

        let request_id = input
            .request_id
            .map_or_else(|| uuid::Uuid::new_v4().to_string(), str::to_string);
        let mode = input.mode.unwrap_or("http-reqresp@v0");
        let spec_version = input.spec_version.unwrap_or("0.1");
        let endpoint = format!("{}/v1/cap", route.worker_url.trim_end_matches('/'));
        let timeout = input.timeout.unwrap_or(Duration::from_secs(60));

        // attempt_once mints fresh + POSTs to the orch. The retry on
        // INVALID_RECIPIENT_RAND MUST mint a new ticket; replaying the
        // rejected one would just be rejected again. The retry also burns
        // a fresh Idempotency-Key so the gateway's mint-idempotency ledger
        // doesn't replay the rejected attempt.
        let body_clone = input.body.clone();
        let attempt_once = async |retry: bool,
                                  body_kind: &JobBody<'_>|
               -> Result<AttemptOutcome, OpenClearinghouseError> {
            let idempotency_key = if retry { None } else { input.idempotency_key };
            let mint = self
                .mint_payment(MintPaymentInput {
                    capability: input.capability,
                    offering: input.offering,
                    work_units: input.work_units,
                    idempotency_key,
                })
                .await?;

            let mut req = self
                .http
                .post(&endpoint)
                .timeout(timeout)
                .header("Livepeer-Capability", input.capability)
                .header("Livepeer-Offering", input.offering)
                .header("Livepeer-Payment", &mint.payment_bytes)
                .header("Livepeer-Mode", mode)
                .header("Livepeer-Spec-Version", spec_version)
                .header("Livepeer-Request-Id", &request_id);
            match body_kind {
                JobBody::Json(v) => {
                    req = req.json(v);
                }
                JobBody::Bytes(b) => {
                    let ct = input.content_type.unwrap_or("application/octet-stream");
                    req = req.header(header::CONTENT_TYPE, ct).body(b.to_vec());
                }
            }
            let res = req.send().await?;
            let status = res.status().as_u16();
            let headers: Vec<(String, String)> = res
                .headers()
                .iter()
                .filter_map(|(k, v)| v.to_str().ok().map(|s| (k.to_string(), s.to_string())))
                .collect();
            let body_bytes = res.bytes().await?.to_vec();
            Ok((mint, status, headers, body_bytes))
        };

        let (mut mint, mut status, mut headers, mut body_bytes) =
            attempt_once(false, &body_clone).await?;

        // Orch session rotation: 401 + INVALID_RECIPIENT_RAND → mint fresh,
        // retry once.
        if status == 401
            && std::str::from_utf8(&body_bytes).is_ok_and(|s| s.contains("INVALID_RECIPIENT_RAND"))
        {
            (mint, status, headers, body_bytes) = attempt_once(true, &body_clone).await?;
        }

        let ctype = headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case("content-type"))
            .map_or("", |(_, v)| v.as_str());
        let body = if ctype.contains("json") && !body_bytes.is_empty() {
            serde_json::from_slice::<Value>(&body_bytes).unwrap_or(Value::Null)
        } else {
            Value::String(String::from_utf8_lossy(&body_bytes).into_owned())
        };
        Ok(JobResult {
            body,
            status,
            payment_id: mint.payment_id,
            recipient_eth_address: mint.recipient_eth_address,
            request_id,
            raw_headers: headers,
        })
    }

    pub async fn report_usage(
        &self,
        input: ReportUsageInput<'_>,
    ) -> Result<UsageReportResult, OpenClearinghouseError> {
        let body = serde_json::json!({
            "payment_id": input.payment_id,
            "actual_work_units": input.actual_work_units,
        });
        self.send(
            Method::POST,
            "/v1/usage/report",
            Some(body),
            input.idempotency_key,
        )
        .await
    }

    // ---- internals ----

    async fn send<R: for<'de> Deserialize<'de>>(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
        idempotency_key: Option<&str>,
    ) -> Result<R, OpenClearinghouseError> {
        let mut req = self
            .http
            .request(method, format!("{}{}", self.base_url, path));
        if let Some(b) = body {
            req = req.json(&b);
        }
        if let Some(k) = idempotency_key {
            req = req.header("Idempotency-Key", k);
        }
        let res = req.send().await?;
        let status = res.status();
        let retry_after = res
            .headers()
            .get(header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse::<u64>().ok());
        let text = res.text().await?;
        let body: Option<Value> = if text.is_empty() {
            None
        } else {
            Some(
                serde_json::from_str(&text)
                    .unwrap_or_else(|_| serde_json::json!({ "detail": text })),
            )
        };

        if status.is_success() {
            let body = body.unwrap_or(Value::Null);
            return serde_json::from_value::<R>(body)
                .map_err(|e| OpenClearinghouseError::Config(format!("decode response: {e}")));
        }
        Err(from_response(status.as_u16(), retry_after, body))
    }
}

fn urlencoded(s: &str) -> String {
    // Minimal percent-encoding for query values; Livepeer Open Clearinghouse capabilities
    // include `:` and `-`, no spaces in practice.
    s.replace(':', "%3A").replace(' ', "%20")
}
