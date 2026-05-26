"""Mock LOC HTTP server for SDK conformance testing.

The mock loads a JSON scenario file describing canned per-endpoint
responses, records every inbound request in an in-process call log,
and exposes a small ``/_test/*`` control surface so runners can
inspect what their SDK actually sent.

Designed to be language-agnostic: any SDK can drive it over plain
HTTP. The Python runner is the reference; TS / Go / Rust runners
follow the same wire protocol.
"""
