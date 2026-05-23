//! Reference Rust SDK for the `PymtHouse` payment clearinghouse.
//!
//! See the README + `examples/example.rs` for the mint -> orchestrator
//! -> reconcile flow.

mod client;
mod errors;

pub use client::{
    Capability, Client, ClientOptions, MintPaymentInput, MintResponse, Offering, Orchestrator,
    ReportUsageInput, UsageReportResult,
};
pub use errors::{ErrorKind, PymtHouseError};
