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
    CapStatus, Capability, Client, ClientOptions, JobBody, JobOpenResponse, JobResult,
    JobSettleResponse, Offering, OpenSessionInput, Orchestrator, SessionAxes, SessionHandle,
    SubmitJobInput, SDK_GIT_SHA, SDK_LANG, SDK_VERSION,
};
pub use errors::{ErrorKind, OpenClearinghouseError};
pub use session_runner::{
    BrokerControl, BrokerSession, RefillCallback, RefillEvent, SessionBalance, SessionOutcome,
    SessionRunner, SessionRunnerOptions, WinddownCallback, WinddownEvent,
};
pub use telemetry::{EmitOptions, EmitterConfig, TelemetryEmitter};
