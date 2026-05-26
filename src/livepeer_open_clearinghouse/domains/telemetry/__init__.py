"""Telemetry domain — SDK + server event ingest, storage, retention.

Layered as: config -> repo -> service -> runtime, matching the rest
of the domains. See ``docs/exec-plans/active/002-long-running-sessions.md``
§"SDK telemetry (v1)" for the full design.
"""
