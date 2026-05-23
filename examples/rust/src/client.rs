use std::time::Duration;

use reqwest::{header, Method};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::errors::{from_response, PymtHouseError};

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
    pub service_url: String,
    pub capabilities: Vec<String>,
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

impl Client {
    pub fn new(opts: ClientOptions) -> Result<Self, PymtHouseError> {
        if !opts.api_key.starts_with("pymth_") {
            return Err(PymtHouseError::Config(
                "api_key looks wrong (expected to start with pymth_)".into(),
            ));
        }
        let mut headers = header::HeaderMap::new();
        headers.insert(
            header::HeaderName::from_static("x-api-key"),
            header::HeaderValue::from_str(&opts.api_key)
                .map_err(|e| PymtHouseError::Config(e.to_string()))?,
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

    pub async fn list_capabilities(&self) -> Result<Vec<Capability>, PymtHouseError> {
        let resp: Wrapped<Capability> = self
            .send(Method::GET, "/v1/capabilities", None, None)
            .await?;
        Ok(resp.items)
    }

    pub async fn list_orchestrators(
        &self,
        capability: Option<&str>,
    ) -> Result<Vec<Orchestrator>, PymtHouseError> {
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
    ) -> Result<MintResponse, PymtHouseError> {
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

    pub async fn report_usage(
        &self,
        input: ReportUsageInput<'_>,
    ) -> Result<UsageReportResult, PymtHouseError> {
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
    ) -> Result<R, PymtHouseError> {
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
                .map_err(|e| PymtHouseError::Config(format!("decode response: {e}")));
        }
        Err(from_response(status.as_u16(), retry_after, body))
    }
}

fn urlencoded(s: &str) -> String {
    // Minimal percent-encoding for query values; PymtHouse capabilities
    // include `:` and `-`, no spaces in practice.
    s.replace(':', "%3A").replace(' ', "%20")
}
