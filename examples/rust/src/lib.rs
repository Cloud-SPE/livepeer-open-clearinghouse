//! Reference Rust SDK for the `Livepeer Open Clearinghouse` payment
//! clearinghouse in handoff mode (exec-plan 002).
//!
//! See the README + `examples/example.rs` for the full job + session
//! flow.

mod client;
mod errors;

pub use client::{
    Capability, CapStatus, Client, ClientOptions, JobBody, JobOpenResponse, JobResult,
    JobSettleResponse, Offering, OpenSessionInput, Orchestrator, SessionHandle, SubmitJobInput,
    SDK_GIT_SHA, SDK_LANG, SDK_VERSION,
};
pub use errors::{ErrorKind, OpenClearinghouseError};
