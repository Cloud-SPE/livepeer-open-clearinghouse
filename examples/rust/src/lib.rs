//! Reference Rust SDK for the `Livepeer Open Clearinghouse` payment
//! clearinghouse in handoff mode (exec-plan 002).
//!
//! See the README + `examples/example.rs` for the full job + session
//! flow.

mod client;
mod errors;
mod session_runner;
mod telemetry;

pub use client::{
    Capability, CapStatus, Client, ClientOptions, JobBody, JobOpenResponse, JobResult,
    JobSettleResponse, Offering, OpenSessionInput, Orchestrator, SessionHandle, SubmitJobInput,
    SDK_GIT_SHA, SDK_LANG, SDK_VERSION,
};
pub use errors::{ErrorKind, OpenClearinghouseError};
pub use session_runner::{
    bounded_modes, http_topup_modes, ws_topup_modes, RefillCallback, RefillEvent, SessionOutcome,
    SessionRunner, SessionRunnerOptions, WinddownCallback, WinddownEvent,
    MODE_LIVE_SESSION_GATEWAY_INGEST, MODE_LIVE_SESSION_REMOTE_RUNNER,
    MODE_RTMP_INGRESS_HLS_EGRESS, MODE_SESSION_CONTROL_PLUS_MEDIA, MODE_WS_REALTIME,
};
pub use telemetry::{EmitOptions, EmitterConfig, TelemetryEmitter};
