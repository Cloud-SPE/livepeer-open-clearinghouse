//! `paid-session/v1` broker control driver with idempotent automatic refills.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine as _;
use futures_util::{SinkExt as _, StreamExt as _};
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::{mpsc, Mutex};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::client::IntoClientRequest as _;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use crate::client::{CapStatus, Client, SessionHandle};
use crate::errors::OpenClearinghouseError;

#[derive(Debug, Clone, Deserialize)]
pub struct SessionBalance {
    pub status: String,
    pub claimed_units: u64,
    pub debited_units: u64,
    pub unit: String,
    pub runway_units: u64,
    pub runway_seconds_estimate: Option<u64>,
    pub will_refuse_next_refill: bool,
}

impl SessionBalance {
    fn validate(self) -> Result<Self, OpenClearinghouseError> {
        if !matches!(self.status.as_str(), "ok" | "low" | "exhausted") {
            return Err(OpenClearinghouseError::broker_protocol(
                "malformed_balance",
                format!("invalid session balance status {}", self.status),
            ));
        }
        Ok(self)
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct BrokerControl {
    pub status_url: String,
    pub topup_url: String,
    pub end_url: String,
    pub events_ws: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct BrokerRuntime {
    schema: String,
    public: Value,
    #[serde(default)]
    grants: Vec<Value>,
}

#[derive(Debug, Clone, Deserialize)]
struct BrokerLease {
    expires_at: String,
}

#[derive(Debug, Clone, Deserialize)]
struct BrokerOpenResponse {
    session_id: String,
    work_id: String,
    state: String,
    runtime: BrokerRuntime,
    credential: String,
    lease: BrokerLease,
    balance: SessionBalance,
    control: BrokerControl,
}

#[derive(Debug, Clone)]
pub struct BrokerSession {
    pub session_id: String,
    pub work_id: String,
    pub state: String,
    pub runtime_schema: String,
    pub runtime_public: Value,
    pub grants: Vec<Value>,
    pub credential: String,
    pub lease_expires_at: String,
    pub balance: SessionBalance,
    pub control: BrokerControl,
}

#[derive(Debug, Clone, Deserialize)]
struct RefillResponse {
    work_id: String,
    request_id: String,
    refill_seq: Option<u64>,
    payment_envelope: String,
    expected_value_wei: Option<u64>,
    funded_value_wei: Option<u64>,
    cap_status: Option<CapStatus>,
    rebind_from: Option<String>,
}

#[derive(Debug, Clone)]
pub struct RefillEvent {
    pub refill_seq: Option<u64>,
    pub expected_value_wei: Option<u64>,
    pub funded_value_wei: Option<u64>,
    pub cap_status: Option<CapStatus>,
    pub error: Option<Arc<OpenClearinghouseError>>,
}

#[derive(Debug, Clone)]
pub struct WinddownEvent {
    pub reason: String,
    pub projected_end_at: Option<String>,
}

pub type RefillCallback =
    Arc<dyn Fn(RefillEvent) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;
pub type WinddownCallback =
    Arc<dyn Fn(WinddownEvent) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

pub struct SessionRunnerOptions {
    pub client: Client,
    pub handle: SessionHandle,
    pub on_refill_succeeded: Option<RefillCallback>,
    pub on_refill_refused: Option<RefillCallback>,
    pub on_winddown_warning: Option<WinddownCallback>,
}

impl SessionRunnerOptions {
    #[must_use]
    pub fn new(client: Client, handle: SessionHandle) -> Self {
        Self {
            client,
            handle,
            on_refill_succeeded: None,
            on_refill_refused: None,
            on_winddown_warning: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SessionOutcome {
    pub outcome: String,
    pub billed_value_wei: u64,
    pub refund_wei: u64,
}

struct Inner {
    client: Client,
    handle: SessionHandle,
    broker: BrokerSession,
    on_refill_succeeded: Option<RefillCallback>,
    on_refill_refused: Option<RefillCallback>,
    on_winddown_warning: Option<WinddownCallback>,
    ws_tx: Option<mpsc::UnboundedSender<Message>>,
    pending_refill_key: Option<String>,
    pending_refill: Option<RefillResponse>,
    final_settle: Option<SessionOutcome>,
    close_started: bool,
}

pub struct SessionRunner {
    inner: Arc<Mutex<Inner>>,
    listener: Option<JoinHandle<()>>,
}

impl SessionRunner {
    pub async fn start(opts: SessionRunnerOptions) -> Result<Self, OpenClearinghouseError> {
        let broker = open_broker_session(&opts.handle).await?;
        let events_ws = broker.control.events_ws.clone();
        let inner = Arc::new(Mutex::new(Inner {
            client: opts.client,
            handle: opts.handle,
            broker,
            on_refill_succeeded: opts.on_refill_succeeded,
            on_refill_refused: opts.on_refill_refused,
            on_winddown_warning: opts.on_winddown_warning,
            ws_tx: None,
            pending_refill_key: None,
            pending_refill: None,
            final_settle: None,
            close_started: false,
        }));
        let listener = if let Some(url) = events_ws {
            Some(open_events_ws(inner.clone(), &url).await?)
        } else {
            None
        };
        Ok(Self { inner, listener })
    }

    pub async fn broker_session(&self) -> BrokerSession {
        self.inner.lock().await.broker.clone()
    }

    pub async fn outcome(&self) -> Option<SessionOutcome> {
        self.inner.lock().await.final_settle.clone()
    }

    pub async fn status(&self) -> Result<Value, OpenClearinghouseError> {
        let (url, credential) = {
            let inner = self.inner.lock().await;
            (
                inner.broker.control.status_url.clone(),
                inner.broker.credential.clone(),
            )
        };
        let response = reqwest::Client::new()
            .get(url)
            .bearer_auth(credential)
            .send()
            .await?
            .error_for_status()?;
        Ok(response.json().await?)
    }

    pub async fn on_balance(&self, balance: SessionBalance) {
        handle_balance(&self.inner, balance).await;
    }

    /// End at the broker before finalizing LOC accounting. Idempotent.
    pub async fn close(&self, actual_units: u64) -> Result<SessionOutcome, OpenClearinghouseError> {
        let (client, session_id, end_url, credential, ws_tx) = {
            let mut inner = self.inner.lock().await;
            if let Some(outcome) = &inner.final_settle {
                return Ok(outcome.clone());
            }
            if inner.close_started {
                drop(inner);
                for _ in 0..50 {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    let outcome = self.inner.lock().await.final_settle.clone();
                    if let Some(outcome) = outcome {
                        return Ok(outcome);
                    }
                }
                return Err(OpenClearinghouseError::transport(
                    "concurrent close did not complete in time",
                ));
            }
            inner.close_started = true;
            (
                inner.client.clone(),
                inner.handle.session_id.clone(),
                inner.broker.control.end_url.clone(),
                inner.broker.credential.clone(),
                inner.ws_tx.take(),
            )
        };
        let end_response = reqwest::Client::new()
            .post(end_url)
            .bearer_auth(credential)
            .json(&serde_json::json!({"reason": "gateway_close"}))
            .send()
            .await?
            .error_for_status()?;
        let encoded_settlement = end_response
            .headers()
            .get("Livepeer-Settlement")
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "broker end response missing Livepeer-Settlement",
                )
            })?;
        let raw_settlement = base64::engine::general_purpose::STANDARD
            .decode(encoded_settlement)
            .map_err(|_| {
                OpenClearinghouseError::broker_protocol(
                    "broker_protocol_error",
                    "broker end response has malformed Livepeer-Settlement",
                )
            })?;
        let settlement: Value = serde_json::from_slice(&raw_settlement).map_err(|_| {
            OpenClearinghouseError::broker_protocol(
                "broker_protocol_error",
                "broker end response has malformed Livepeer-Settlement",
            )
        })?;
        if !settlement.is_object() {
            return Err(OpenClearinghouseError::broker_protocol(
                "broker_protocol_error",
                "broker end response has malformed Livepeer-Settlement",
            ));
        }
        if let Some(tx) = ws_tx {
            let _ = tx.send(Message::Close(None));
        }
        let response = client
            .close_session(&session_id, actual_units, None, settlement)
            .await?;
        let outcome = parse_outcome(&response);
        self.inner.lock().await.final_settle = Some(outcome.clone());
        Ok(outcome)
    }
}

impl Drop for SessionRunner {
    fn drop(&mut self) {
        if let Some(listener) = self.listener.take() {
            listener.abort();
        }
    }
}

async fn open_broker_session(
    handle: &SessionHandle,
) -> Result<BrokerSession, OpenClearinghouseError> {
    let url = format!("{}/v1/session", handle.broker_url.trim_end_matches('/'));
    let response = reqwest::Client::new()
        .post(url)
        .header("Livepeer-Protocol", &handle.protocol)
        .header("Livepeer-Capability", &handle.capability)
        .header("Livepeer-Offering", &handle.offering)
        .header("Livepeer-Request-Id", &handle.request_id)
        .header("Livepeer-Payment", &handle.payment_envelope)
        .json(&serde_json::json!({
            "gateway_session_id": handle.session_id,
            "session_params": handle.session_params,
        }))
        .send()
        .await?
        .error_for_status()?;
    let parsed: BrokerOpenResponse = response.json().await?;
    if parsed.work_id != handle.work_id {
        return Err(OpenClearinghouseError::broker_protocol(
            "work_id_mismatch",
            "broker session work_id does not match the payment",
        ));
    }
    if parsed.runtime.schema != handle.session.descriptor_schema {
        return Err(OpenClearinghouseError::broker_protocol(
            "descriptor_schema_mismatch",
            format!(
                "broker returned descriptor {}; expected {}",
                parsed.runtime.schema, handle.session.descriptor_schema
            ),
        ));
    }
    if !parsed.runtime.public.is_object() || parsed.credential.is_empty() {
        return Err(OpenClearinghouseError::broker_protocol(
            "malformed_session_open",
            "broker returned malformed runtime or credential",
        ));
    }
    Ok(BrokerSession {
        session_id: parsed.session_id,
        work_id: parsed.work_id,
        state: parsed.state,
        runtime_schema: parsed.runtime.schema,
        runtime_public: parsed.runtime.public,
        grants: parsed.runtime.grants,
        credential: parsed.credential,
        lease_expires_at: parsed.lease.expires_at,
        balance: parsed.balance.validate()?,
        control: parsed.control,
    })
}

async fn open_events_ws(
    inner: Arc<Mutex<Inner>>,
    url: &str,
) -> Result<JoinHandle<()>, OpenClearinghouseError> {
    let credential = inner.lock().await.broker.credential.clone();
    let mut request = url
        .into_client_request()
        .map_err(|e| OpenClearinghouseError::transport(format!("events request: {e}")))?;
    request.headers_mut().insert(
        "Authorization",
        HeaderValue::from_str(&format!("Bearer {credential}"))
            .map_err(|e| OpenClearinghouseError::transport(format!("credential header: {e}")))?,
    );
    let (socket, _) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|e| OpenClearinghouseError::transport(format!("events dial: {e}")))?;
    let (mut writer, mut reader) = socket.split();
    let (tx, mut rx) = mpsc::unbounded_channel();
    inner.lock().await.ws_tx = Some(tx);
    tokio::spawn(async move {
        while let Some(message) = rx.recv().await {
            if writer.send(message).await.is_err() {
                break;
            }
        }
    });

    Ok(tokio::spawn(async move {
        while let Some(Ok(Message::Text(text))) = reader.next().await {
            let Ok(payload) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            if payload.get("type").and_then(Value::as_str) != Some("session.balance") {
                continue;
            }
            let Some(balance) = payload.get("balance") else {
                continue;
            };
            let Ok(balance) = serde_json::from_value::<SessionBalance>(balance.clone()) else {
                continue;
            };
            if let Ok(balance) = balance.validate() {
                handle_balance(&inner, balance).await;
            }
        }
    }))
}

async fn handle_balance(inner: &Arc<Mutex<Inner>>, balance: SessionBalance) {
    let (refill_units, winddown) = {
        let state = inner.lock().await;
        if balance.will_refuse_next_refill {
            (
                None,
                Some((
                    state.on_winddown_warning.clone(),
                    "broker_will_refuse_next_refill",
                )),
            )
        } else if balance.status != "low" {
            (None, None)
        } else if state.handle.session.refill == "bounded" {
            (
                None,
                Some((
                    state.on_winddown_warning.clone(),
                    "bounded_runway_exhausting",
                )),
            )
        } else {
            (Some(balance.claimed_units), None)
        }
    };
    if let Some((callback, reason)) = winddown {
        if let Some(callback) = callback {
            callback(WinddownEvent {
                reason: reason.to_string(),
                projected_end_at: None,
            })
            .await;
        }
        return;
    }
    if let Some(observed_units) = refill_units {
        refill(inner, observed_units).await;
    }
}

async fn refill(inner: &Arc<Mutex<Inner>>, observed_units: u64) {
    let (client, session_id, request_id, existing, callback_refused) = {
        let mut state = inner.lock().await;
        let request_id = state
            .pending_refill_key
            .get_or_insert_with(|| uuid::Uuid::new_v4().to_string())
            .clone();
        (
            state.client.clone(),
            state.handle.session_id.clone(),
            request_id,
            state.pending_refill.clone(),
            state.on_refill_refused.clone(),
        )
    };
    let refill = if let Some(existing) = existing {
        existing
    } else {
        let result = client
            .refill_session(
                &session_id,
                Some(observed_units),
                Some(&request_id),
                None,
                None,
            )
            .await;
        match result
            .and_then(|value| serde_json::from_value::<RefillResponse>(value).map_err(Into::into))
        {
            Ok(refill) => {
                inner.lock().await.pending_refill = Some(refill.clone());
                refill
            }
            Err(error) => {
                if let Some(callback) = callback_refused {
                    callback(RefillEvent {
                        refill_seq: None,
                        expected_value_wei: None,
                        funded_value_wei: None,
                        cap_status: None,
                        error: Some(Arc::new(error)),
                    })
                    .await;
                }
                return;
            }
        }
    };

    let (url, credential, callback_succeeded, callback_winddown) = {
        let state = inner.lock().await;
        (
            state.broker.control.topup_url.clone(),
            state.broker.credential.clone(),
            state.on_refill_succeeded.clone(),
            state.on_winddown_warning.clone(),
        )
    };
    let response = post_topup(&url, &credential, &refill).await;
    let Ok(mut response) = response else {
        return;
    };
    let mut accepted_refill = refill;
    if broker_error(&response) == Some("recipient_rotated") {
        if accepted_refill.rebind_from.is_some() {
            end_unrecoverable_rotation(inner, callback_winddown).await;
            return;
        }
        let predecessor = accepted_refill.work_id.clone();
        let replaces_request_id = accepted_refill.request_id.clone();
        let replacement_key = uuid::Uuid::new_v4().to_string();
        inner.lock().await.pending_refill_key = Some(replacement_key.clone());
        let replacement = client
            .refill_session(
                &session_id,
                Some(observed_units),
                Some(&replacement_key),
                Some(&predecessor),
                Some(&replaces_request_id),
            )
            .await
            .and_then(|value| serde_json::from_value::<RefillResponse>(value).map_err(Into::into));
        let Ok(replacement) = replacement else {
            if let Err(error) = replacement {
                if let Some(callback) = callback_refused {
                    callback(RefillEvent {
                        refill_seq: None,
                        expected_value_wei: None,
                        funded_value_wei: None,
                        cap_status: None,
                        error: Some(Arc::new(error)),
                    })
                    .await;
                }
            }
            return;
        };
        inner.lock().await.pending_refill = Some(replacement.clone());
        accepted_refill = replacement;
        let Ok(replacement_response) = post_topup(&url, &credential, &accepted_refill).await else {
            return;
        };
        response = replacement_response;
    }
    if broker_error(&response) == Some("recipient_rotated") {
        end_unrecoverable_rotation(inner, callback_winddown.clone()).await;
        return;
    }
    if broker_error(&response) == Some("rebind_refused") {
        end_unrecoverable_rotation(inner, callback_winddown).await;
        return;
    }
    let Ok(response) = response.error_for_status() else {
        return;
    };
    if accepted_refill.rebind_from.is_some() {
        inner.lock().await.broker.work_id = accepted_refill.work_id.clone();
    }
    let broker_result: Value = match response.json().await {
        Ok(value) => value,
        Err(_) => return,
    };
    if broker_result
        .get("balance")
        .and_then(|value| serde_json::from_value::<SessionBalance>(value.clone()).ok())
        .is_some_and(|balance| balance.will_refuse_next_refill)
    {
        if let Some(callback) = callback_winddown {
            callback(WinddownEvent {
                reason: "broker_will_refuse_next_refill".to_string(),
                projected_end_at: None,
            })
            .await;
        }
    }
    if let Some(callback) = callback_succeeded {
        callback(RefillEvent {
            refill_seq: accepted_refill.refill_seq,
            expected_value_wei: accepted_refill.expected_value_wei,
            funded_value_wei: accepted_refill.funded_value_wei,
            cap_status: accepted_refill.cap_status.clone(),
            error: None,
        })
        .await;
    }
    let mut state = inner.lock().await;
    state.pending_refill_key = None;
    state.pending_refill = None;
}

async fn post_topup(
    url: &str,
    credential: &str,
    refill: &RefillResponse,
) -> Result<reqwest::Response, reqwest::Error> {
    let mut request = reqwest::Client::new()
        .post(url)
        .bearer_auth(credential)
        .header("Livepeer-Payment", &refill.payment_envelope)
        .header("Livepeer-Request-Id", &refill.request_id);
    if let Some(rebind_from) = &refill.rebind_from {
        request = request.header("Livepeer-Rebind-From", rebind_from);
    }
    request.json(&serde_json::json!({})).send().await
}

fn broker_error(response: &reqwest::Response) -> Option<&str> {
    if response.status() != reqwest::StatusCode::CONFLICT {
        return None;
    }
    response
        .headers()
        .get("Livepeer-Error")
        .and_then(|value| value.to_str().ok())
}

async fn end_unrecoverable_rotation(inner: &Arc<Mutex<Inner>>, callback: Option<WinddownCallback>) {
    let mut state = inner.lock().await;
    state.pending_refill_key = None;
    state.pending_refill = None;
    drop(state);
    if let Some(callback) = callback {
        callback(WinddownEvent {
            reason: "payment_unrecoverable".to_string(),
            projected_end_at: None,
        })
        .await;
    }
}

fn parse_outcome(response: &Value) -> SessionOutcome {
    SessionOutcome {
        outcome: response
            .get("outcome")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        billed_value_wei: response
            .get("billed_value_wei")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        refund_wei: response
            .get("refund_wei")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
    }
}
