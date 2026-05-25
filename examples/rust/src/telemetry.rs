//! SDK-side telemetry emitter (Rust SDK).
//!
//! Parity with the Python reference: fire-and-forget, batched,
//! flush-on-critical, bounded buffer with oldest-dropped on overflow,
//! gzip > 1 KiB, 3-attempt exponential backoff. Telemetry is
//! mandatory; no opt-out.

use std::collections::VecDeque;
use std::io::Write;
use std::sync::Arc;
use std::time::Duration;

use flate2::Compression;
use flate2::write::GzEncoder;
use serde::Serialize;
use serde_json::Value;
use tokio::sync::{Mutex, Notify};
use tokio::task::JoinHandle;
use tokio::time::sleep;

pub const DEFAULT_BATCH_SIZE: usize = 100;
pub const DEFAULT_FLUSH_INTERVAL_MS: u64 = 5_000;
pub const DEFAULT_BUFFER_CAP: usize = 10_000;
pub const DEFAULT_MAX_RETRIES: u32 = 3;
pub const DEFAULT_GZIP_THRESHOLD_BYTES: usize = 1024;

const CRITICAL_EVENT_TYPES: &[&str] = &["session.refill_denied", "session.closed"];

#[inline]
fn is_critical_event(event_type: &str) -> bool {
    CRITICAL_EVENT_TYPES.iter().any(|&t| t == event_type) || event_type.ends_with(".error")
}

#[derive(Debug, Clone, Serialize)]
struct BufferedEvent {
    event_type: String,
    event_schema_version: u32,
    correlation_id: Option<String>,
    client_ts: String,
    payload: Value,
}

#[derive(Debug, Clone, Default)]
pub struct EmitOptions {
    pub correlation_id: Option<String>,
    pub payload: Option<Value>,
    pub client_ts: Option<String>,
    pub event_schema_version: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct EmitterConfig {
    pub batch_size: usize,
    pub flush_interval_ms: u64,
    pub buffer_cap: usize,
    pub max_retries: u32,
    pub gzip_threshold_bytes: usize,
}

impl Default for EmitterConfig {
    fn default() -> Self {
        Self {
            batch_size: DEFAULT_BATCH_SIZE,
            flush_interval_ms: DEFAULT_FLUSH_INTERVAL_MS,
            buffer_cap: DEFAULT_BUFFER_CAP,
            max_retries: DEFAULT_MAX_RETRIES,
            gzip_threshold_bytes: DEFAULT_GZIP_THRESHOLD_BYTES,
        }
    }
}

struct EmitterInner {
    buffer: VecDeque<BufferedEvent>,
    dropped: usize,
    closed: bool,
}

/// The SDK-side telemetry emitter. Construct via :func:`new`; the
/// background flush task starts immediately.
pub struct TelemetryEmitter {
    http: reqwest::Client,
    endpoint: String,
    api_key: String,
    sdk_identity: String,
    config: EmitterConfig,
    inner: Arc<Mutex<EmitterInner>>,
    notify: Arc<Notify>,
    // std::sync::Mutex — set synchronously in `new()` so `close()`
    // can always await the loop. tokio::sync::Mutex would force us
    // to defer the install to a spawned task, opening a race where
    // close() runs first.
    flush_task: std::sync::Mutex<Option<JoinHandle<()>>>,
}

impl TelemetryEmitter {
    /// Construct + start the background flush loop.
    #[must_use]
    pub fn new(
        http: reqwest::Client,
        base_url: &str,
        api_key: String,
        sdk_identity: String,
        config: EmitterConfig,
    ) -> Arc<Self> {
        let endpoint = format!("{}/v1/telemetry", base_url.trim_end_matches('/'));
        let inner = Arc::new(Mutex::new(EmitterInner {
            buffer: VecDeque::with_capacity(config.batch_size),
            dropped: 0,
            closed: false,
        }));
        let notify = Arc::new(Notify::new());
        let emitter = Arc::new(Self {
            http,
            endpoint,
            api_key,
            sdk_identity,
            config,
            inner: inner.clone(),
            notify: notify.clone(),
            flush_task: std::sync::Mutex::new(None),
        });
        let task = tokio::spawn(Self::flush_loop(emitter.clone()));
        // Synchronous install so close() always has a handle to wait on.
        if let Ok(mut slot) = emitter.flush_task.lock() {
            *slot = Some(task);
        }
        emitter
    }

    /// Append one event. Never blocks beyond mutex acquisition.
    pub async fn emit(&self, event_type: impl Into<String>, opts: EmitOptions) {
        let event_type = event_type.into();
        let event = BufferedEvent {
            event_type: event_type.clone(),
            event_schema_version: opts.event_schema_version.unwrap_or(1),
            correlation_id: opts.correlation_id,
            client_ts: opts.client_ts.unwrap_or_else(now_rfc3339),
            payload: opts.payload.unwrap_or(Value::Object(serde_json::Map::new())),
        };
        let mut inner = self.inner.lock().await;
        if inner.closed {
            return;
        }
        if inner.buffer.len() == self.config.buffer_cap {
            inner.buffer.pop_front();
            inner.dropped += 1;
            tracing_warn(&format!(
                "[telemetry] buffer full; dropped oldest event (total dropped={})",
                inner.dropped
            ));
        }
        inner.buffer.push_back(event);
        let buf_len = inner.buffer.len();
        drop(inner);
        if is_critical_event(&event_type) || buf_len >= self.config.batch_size {
            self.notify.notify_one();
        }
    }

    /// Drain the buffer with one last flush. Idempotent.
    pub async fn close(&self) {
        {
            let mut inner = self.inner.lock().await;
            if inner.closed {
                return;
            }
            inner.closed = true;
        }
        self.notify.notify_one();
        let handle = self.flush_task.lock().ok().and_then(|mut s| s.take());
        if let Some(handle) = handle {
            // Wait up to a few seconds for the loop to drain.
            let _ = tokio::time::timeout(Duration::from_secs(5), handle).await;
        }
    }

    /// Current count of unflushed events. Useful for tests.
    pub async fn buffer_size(&self) -> usize {
        self.inner.lock().await.buffer.len()
    }

    /// Count of events dropped due to overflow.
    pub async fn dropped(&self) -> usize {
        self.inner.lock().await.dropped
    }

    async fn flush_loop(self: Arc<Self>) {
        let interval = Duration::from_millis(self.config.flush_interval_ms);
        loop {
            let timeout = tokio::time::sleep(interval);
            tokio::select! {
                () = self.notify.notified() => {}
                () = timeout => {}
            }
            self.flush_once().await;
            if self.inner.lock().await.closed {
                // One final drain after the close signal.
                self.flush_once().await;
                return;
            }
        }
    }

    async fn flush_once(&self) {
        let batch: Vec<BufferedEvent> = {
            let mut inner = self.inner.lock().await;
            if inner.buffer.is_empty() {
                return;
            }
            inner.buffer.drain(..).collect()
        };
        let body_json = match serde_json::to_vec(&serde_json::json!({ "events": batch })) {
            Ok(v) => v,
            Err(e) => {
                tracing_warn(&format!("[telemetry] marshal failed: {e}"));
                return;
            }
        };
        let (body, gzipped) = if body_json.len() > self.config.gzip_threshold_bytes {
            match gzip_bytes(&body_json) {
                Ok(b) => (b, true),
                Err(_) => (body_json, false),
            }
        } else {
            (body_json, false)
        };
        self.send_with_retry(body, gzipped, batch.len()).await;
    }

    async fn send_with_retry(&self, body: Vec<u8>, gzipped: bool, event_count: usize) {
        let mut backoff = Duration::from_millis(500);
        for attempt in 1..=self.config.max_retries {
            let mut req = self
                .http
                .post(&self.endpoint)
                .header("Content-Type", "application/json")
                .header("X-API-Key", &self.api_key)
                .header("Livepeer-Open-Clearinghouse-SDK", &self.sdk_identity)
                .body(body.clone());
            if gzipped {
                req = req.header("Content-Encoding", "gzip");
            }
            if let Ok(resp) = req.send().await {
                let status = resp.status().as_u16();
                if status < 500 && status != 429 {
                    return;
                }
            }
            if attempt < self.config.max_retries {
                sleep(backoff).await;
                backoff *= 2;
            }
        }
        tracing_warn(&format!(
            "[telemetry] flush dropped {event_count} events after retries"
        ));
    }
}

fn now_rfc3339() -> String {
    // RFC 3339 without an external dep — use the chrono-equivalent
    // surface from std::time + manual format. The python/go/ts SDKs
    // all stamp ISO 8601; matching ".isoformat()" is good enough.
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs();
    let nanos = now.subsec_nanos();
    // Format as YYYY-MM-DDTHH:MM:SS.nnnnnnnnnZ — small manual helper.
    let (year, month, day, hour, minute, second) = unix_to_components(secs);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{nanos:09}Z")
}

// Minimal civil-time conversion. Good enough for telemetry client_ts.
// Source: Howard Hinnant's date algorithms (public domain).
fn unix_to_components(secs: u64) -> (u32, u32, u32, u32, u32, u32) {
    #[allow(clippy::cast_possible_truncation, clippy::cast_possible_wrap)]
    let secs_i = secs as i64;
    let days = secs_i.div_euclid(86_400);
    let rem = secs_i.rem_euclid(86_400);
    let hour = (rem / 3600) as u32;
    let minute = ((rem % 3600) / 60) as u32;
    let second = (rem % 60) as u32;
    // days since 1970-01-01
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    (year as u32, m as u32, d as u32, hour, minute, second)
}

fn gzip_bytes(input: &[u8]) -> std::io::Result<Vec<u8>> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(input)?;
    encoder.finish()
}

fn tracing_warn(msg: &str) {
    // Centralized so callers don't need to import the logger. The
    // crate doesn't pull `tracing` today — fall back to eprintln for
    // visibility without a new dep.
    eprintln!("{msg}");
}
