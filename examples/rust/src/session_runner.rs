//! `SessionRunner` — automatic refill loop for case-(d-extensible) modes.
//!
//! Companion to the Python/TS/Go `SessionRunner`. See
//! `examples/python/src/livepeer_open_clearinghouse_sdk/session_runner.py`
//! for the canonical reference and design rationale.
//!
//! Usage
//!
//! ```ignore
//! let handle = client.open_session(...).await?;
//! let runner = SessionRunner::start(SessionRunnerOptions {
//!     client: client.clone(),
//!     handle,
//!     on_refill_succeeded: Some(Box::new(|e| Box::pin(async move {
//!         tracing::info!("refilled: {:?}", e);
//!     }))),
//!     on_refill_refused: None,
//!     on_winddown_warning: None,
//! }).await?;
//! // ... use the session ...
//! let outcome = runner.close(0).await?;
//! ```

use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt as _, StreamExt as _};
use serde::Deserialize;
use serde_json::Value;
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::client::IntoClientRequest as _;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use crate::client::{CapStatus, Client, SessionHandle};
use crate::errors::OpenClearinghouseError;

/// Mode identifiers — kept as `const`s mirrored against the design doc.
pub const MODE_WS_REALTIME: &str = "ws-realtime@v0";
pub const MODE_SESSION_CONTROL_PLUS_MEDIA: &str = "session-control-plus-media@v0";
pub const MODE_RTMP_INGRESS_HLS_EGRESS: &str = "rtmp-ingress-hls-egress@v0";
pub const MODE_LIVE_SESSION_REMOTE_RUNNER: &str = "live-session-remote-runner@v0";
pub const MODE_LIVE_SESSION_GATEWAY_INGEST: &str = "live-session-gateway-ingest@v0";

#[must_use]
pub fn bounded_modes() -> HashSet<&'static str> {
    HashSet::from([MODE_WS_REALTIME])
}

#[must_use]
pub fn ws_topup_modes() -> HashSet<&'static str> {
    HashSet::from([MODE_SESSION_CONTROL_PLUS_MEDIA, MODE_RTMP_INGRESS_HLS_EGRESS])
}

#[must_use]
pub fn http_topup_modes() -> HashSet<&'static str> {
    HashSet::from([
        MODE_LIVE_SESSION_REMOTE_RUNNER,
        MODE_LIVE_SESSION_GATEWAY_INGEST,
    ])
}

#[derive(Debug, Clone, Deserialize)]
pub struct RefillResponse {
    pub refill_seq: Option<u64>,
    pub payment_envelope: String,
    pub expected_value_wei: Option<u64>,
    pub funded_value_wei: Option<u64>,
    pub cap_status: Option<CapStatus>,
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

/// Boxed async callback signatures. Each callback returns a pinned
/// future to allow async user code without a generic explosion.
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
    /// Auto-finalize on broker disconnect (default true).
    pub auto_close_on_disconnect: bool,
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
            auto_close_on_disconnect: true,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SessionOutcome {
    pub outcome: String,
    pub billed_value_wei: u64,
    pub refund_wei: u64,
}

/// Inner state under the mutex so the runner is `Send + Sync`.
#[allow(clippy::struct_excessive_bools)]
struct Inner {
    client: Client,
    handle: SessionHandle,
    on_refill_succeeded: Option<RefillCallback>,
    on_refill_refused: Option<RefillCallback>,
    on_winddown_warning: Option<WinddownCallback>,
    auto_close: bool,
    ws_tx: Option<mpsc::UnboundedSender<Message>>,
    control_topup_url: Option<String>,
    final_settle: Option<SessionOutcome>,
    close_started: bool,
    is_bounded: bool,
    uses_ws_topup: bool,
    uses_http_topup: bool,
}

pub struct SessionRunner {
    inner: Arc<Mutex<Inner>>,
    listener: Option<JoinHandle<()>>,
}

impl SessionRunner {
    /// Open the broker-side connection appropriate to the mode and
    /// return a started runner. Errors at this stage are configuration
    /// problems (unsupported mode, broker unreachable).
    pub async fn start(opts: SessionRunnerOptions) -> Result<Self, OpenClearinghouseError> {
        let mode = opts.handle.mode.clone();
        let is_bounded = bounded_modes().contains(mode.as_str());
        let uses_ws_topup = ws_topup_modes().contains(mode.as_str());
        let uses_http_topup = http_topup_modes().contains(mode.as_str());
        if !is_bounded && !uses_ws_topup && !uses_http_topup {
            return Err(OpenClearinghouseError::invalid_argument(format!(
                "SessionRunner: unsupported mode {mode}"
            )));
        }
        let inner = Inner {
            client: opts.client,
            handle: opts.handle,
            on_refill_succeeded: opts.on_refill_succeeded,
            on_refill_refused: opts.on_refill_refused,
            on_winddown_warning: opts.on_winddown_warning,
            auto_close: opts.auto_close_on_disconnect,
            ws_tx: None,
            control_topup_url: None,
            final_settle: None,
            close_started: false,
            is_bounded,
            uses_ws_topup,
            uses_http_topup,
        };
        let inner = Arc::new(Mutex::new(inner));
        let listener = if is_bounded || uses_ws_topup {
            Some(open_ws(inner.clone()).await?)
        } else {
            open_live_session(inner.clone()).await?;
            None
        };
        Ok(Self { inner, listener })
    }

    pub async fn outcome(&self) -> Option<SessionOutcome> {
        self.inner.lock().await.final_settle.clone()
    }

    /// Trigger the balance-low handling explicitly. For HTTP-topup modes
    /// where the customer's media-plane code observes balance-low.
    pub async fn on_balance_low(
        &self,
        observed_consumed_units: Option<u64>,
        projected_end_at: Option<String>,
    ) {
        handle_balance_low(&self.inner, observed_consumed_units, projected_end_at).await;
    }

    /// Close the session and finalize accounting on LOC. Idempotent;
    /// subsequent calls (including concurrent close-from-disconnect)
    /// return the cached outcome.
    pub async fn close(&self, actual_units: u64) -> Result<SessionOutcome, OpenClearinghouseError> {
        let (client, session_id, ws_tx) = {
            let mut inner = self.inner.lock().await;
            if let Some(s) = &inner.final_settle {
                return Ok(s.clone());
            }
            if inner.close_started {
                // Another close is in flight — fall through, but
                // don't drive a second LOC call; wait briefly then
                // re-check.
                drop(inner);
                for _ in 0..50 {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    let settled = self.inner.lock().await.final_settle.clone();
                    if let Some(s) = settled {
                        return Ok(s);
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
                inner.ws_tx.take(),
            )
        };
        if let Some(tx) = ws_tx {
            let _ = tx.send(Message::Close(None));
        }
        let resp = client.close_session(&session_id, actual_units, None, None).await?;
        let outcome = SessionOutcome {
            outcome: resp
                .get("outcome")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            billed_value_wei: resp
                .get("billed_value_wei")
                .and_then(Value::as_u64)
                .unwrap_or(0),
            refund_wei: resp.get("refund_wei").and_then(Value::as_u64).unwrap_or(0),
        };
        let mut inner = self.inner.lock().await;
        inner.final_settle = Some(outcome.clone());
        drop(inner);
        Ok(outcome)
    }
}

impl Drop for SessionRunner {
    fn drop(&mut self) {
        if let Some(h) = self.listener.take() {
            h.abort();
        }
    }
}

async fn open_ws(
    inner: Arc<Mutex<Inner>>,
) -> Result<JoinHandle<()>, OpenClearinghouseError> {
    let (broker_url, envelope, mode) = {
        let g = inner.lock().await;
        (
            g.handle.broker_url.clone(),
            g.handle.payment_envelope.clone(),
            g.handle.mode.clone(),
        )
    };
    let mut req = broker_url
        .into_client_request()
        .map_err(|e| OpenClearinghouseError::transport(format!("ws request: {e}")))?;
    let headers = req.headers_mut();
    headers.insert(
        "Livepeer-Payment",
        HeaderValue::from_str(&envelope)
            .map_err(|e| OpenClearinghouseError::transport(format!("header: {e}")))?,
    );
    headers.insert(
        "Livepeer-Mode",
        HeaderValue::from_str(&mode)
            .map_err(|e| OpenClearinghouseError::transport(format!("header: {e}")))?,
    );

    let (ws, _resp) = tokio_tungstenite::connect_async(req)
        .await
        .map_err(|e| OpenClearinghouseError::transport(format!("ws dial: {e}")))?;

    let (mut writer, mut reader) = ws.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<Message>();
    {
        let mut g = inner.lock().await;
        g.ws_tx = Some(tx);
    }

    // Writer task: forwards channel -> WS.
    let _writer_task = tokio::spawn(async move {
        while let Some(msg) = rx.recv().await {
            if writer.send(msg).await.is_err() {
                break;
            }
        }
        let _ = writer.close().await;
    });

    // Reader task: WS -> balance-low handler.
    let inner_for_reader = inner.clone();
    let handle = tokio::spawn(async move {
        listen_ws(inner_for_reader, &mut reader).await;
    });

    Ok(handle)
}

async fn listen_ws(
    inner: Arc<Mutex<Inner>>,
    reader: &mut futures_util::stream::SplitStream<WebSocketStream<MaybeTlsStream<TcpStream>>>,
) {
    while let Some(msg) = reader.next().await {
        let Ok(Message::Text(text)) = msg else { continue };
        let Ok(payload) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let kind = payload.get("type").and_then(Value::as_str).unwrap_or("");
        if kind == "session.balance.low" || kind == "Livepeer-Balance-Low" {
            let observed = payload
                .get("observed_consumed_units")
                .and_then(Value::as_u64);
            let projected = payload
                .get("projected_end_at")
                .and_then(Value::as_str)
                .map(str::to_string);
            handle_balance_low(&inner, observed, projected).await;
        }
    }
    // Disconnected — auto-close if asked and nobody else owns it.
    let (auto, client, session_id) = {
        let mut g = inner.lock().await;
        if g.final_settle.is_some() || g.close_started {
            return;
        }
        if !g.auto_close {
            return;
        }
        g.close_started = true;
        (
            g.auto_close,
            g.client.clone(),
            g.handle.session_id.clone(),
        )
    };
    if !auto {
        return;
    }
    if let Ok(resp) = client.close_session(&session_id, 0, None, None).await {
        let outcome = SessionOutcome {
            outcome: resp
                .get("outcome")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            billed_value_wei: resp
                .get("billed_value_wei")
                .and_then(Value::as_u64)
                .unwrap_or(0),
            refund_wei: resp.get("refund_wei").and_then(Value::as_u64).unwrap_or(0),
        };
        let mut g = inner.lock().await;
        if g.final_settle.is_none() {
            g.final_settle = Some(outcome);
        }
    }
}

async fn open_live_session(inner: Arc<Mutex<Inner>>) -> Result<(), OpenClearinghouseError> {
    let (broker_url, envelope, mode) = {
        let g = inner.lock().await;
        (
            g.handle.broker_url.clone(),
            g.handle.payment_envelope.clone(),
            g.handle.mode.clone(),
        )
    };
    let url = format!("{}/v1/cap", broker_url.trim_end_matches('/'));
    let res = reqwest::Client::new()
        .post(&url)
        .header("Livepeer-Payment", envelope)
        .header("Livepeer-Mode", mode)
        .header("Content-Type", "application/json")
        .body("{}")
        .send()
        .await
        .map_err(|e| OpenClearinghouseError::transport(format!("broker session-open: {e}")))?;
    if !res.status().is_success() {
        return Err(OpenClearinghouseError::transport(format!(
            "broker session-open failed: {}",
            res.status().as_u16()
        )));
    }
    let body: Value = res
        .json()
        .await
        .map_err(|e| OpenClearinghouseError::transport(format!("session-open body: {e}")))?;
    let topup_url = body
        .get("control")
        .and_then(|c| c.get("topup_url"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| OpenClearinghouseError::transport("missing control.topup_url"))?;
    let mut g = inner.lock().await;
    g.control_topup_url = Some(topup_url);
    drop(g);
    Ok(())
}

#[allow(clippy::too_many_lines)]
async fn handle_balance_low(
    inner: &Arc<Mutex<Inner>>,
    observed_consumed_units: Option<u64>,
    projected_end_at: Option<String>,
) {
    let (
        client,
        session_id,
        is_bounded,
        uses_ws_topup,
        uses_http_topup,
        on_refill_succeeded,
        on_refill_refused,
        on_winddown_warning,
        ws_tx,
        topup_url,
    ) = {
        let g = inner.lock().await;
        (
            g.client.clone(),
            g.handle.session_id.clone(),
            g.is_bounded,
            g.uses_ws_topup,
            g.uses_http_topup,
            g.on_refill_succeeded.clone(),
            g.on_refill_refused.clone(),
            g.on_winddown_warning.clone(),
            g.ws_tx.clone(),
            g.control_topup_url.clone(),
        )
    };

    if is_bounded {
        if let Some(cb) = on_winddown_warning {
            cb(WinddownEvent {
                reason: "ws_session_exhausting".to_string(),
                projected_end_at,
            })
            .await;
        }
        return;
    }

    let refill_result = client
        .refill_session(&session_id, observed_consumed_units)
        .await;
    let refill_value = match refill_result {
        Ok(v) => v,
        Err(e) => {
            if let Some(cb) = on_refill_refused {
                cb(RefillEvent {
                    refill_seq: None,
                    expected_value_wei: None,
                    funded_value_wei: None,
                    cap_status: None,
                    error: Some(Arc::new(e)),
                })
                .await;
            }
            return;
        }
    };

    let Ok(refill) = serde_json::from_value::<RefillResponse>(refill_value) else {
        return;
    };

    if uses_ws_topup {
        if let Some(tx) = ws_tx {
            let frame = serde_json::json!({
                "type": "session.topup",
                "body": { "payment_header": refill.payment_envelope.clone() },
            });
            let _ = tx.send(Message::Text(frame.to_string()));
        }
    } else if uses_http_topup {
        if let Some(url) = topup_url {
            let body = serde_json::json!({"gateway_session_id": session_id});
            let _ = reqwest::Client::new()
                .post(&url)
                .header("Livepeer-Payment", refill.payment_envelope.clone())
                .header("Content-Type", "application/json")
                .body(body.to_string())
                .send()
                .await;
        }
    }

    let cap_status = refill.cap_status.clone();
    if let Some(cb) = on_refill_succeeded {
        cb(RefillEvent {
            refill_seq: refill.refill_seq,
            expected_value_wei: refill.expected_value_wei,
            funded_value_wei: refill.funded_value_wei,
            cap_status: cap_status.clone(),
            error: None,
        })
        .await;
    }

    if let Some(cs) = cap_status {
        if cs.will_refuse_next_refill {
            if let Some(cb) = on_winddown_warning {
                cb(WinddownEvent {
                    reason: cs.winddown_reason.unwrap_or_else(|| "cap_imminent".to_string()),
                    projected_end_at: None,
                })
                .await;
            }
        }
    }
}
