"""Domain-local constants for telemetry.

Settings live on the central `Settings` object; this file holds
literals + enums that aren't operator-tunable.
"""

from __future__ import annotations

# Current event-schema major version. Bumped when the universal fields
# change shape. SDKs MUST send the version they're emitting so future
# ingest can route on it.
CURRENT_SCHEMA_VERSION: int = 1

# Allowed values for the `source` column. SDK-emitted events come in
# via POST /v1/telemetry; server-emitted come from LOC's own runtime.
SOURCE_SDK = "sdk"
SOURCE_SERVER = "server"
ALLOWED_SOURCES: frozenset[str] = frozenset({SOURCE_SDK, SOURCE_SERVER})

# Hard limit on inbound batch size — defends against an SDK shipping
# unbounded buffers in one call. Tuned conservatively; the SDK contract
# is "flush at 100 events," so 1000 is 10x headroom.
MAX_BATCH_SIZE: int = 1000

# Hard ceiling on the per-event payload size in bytes. Telemetry is
# metadata, not body content; anything past this is malformed.
MAX_PAYLOAD_BYTES: int = 16 * 1024  # 16 KiB
