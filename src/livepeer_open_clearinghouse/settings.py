"""Application configuration — env -> typed Settings.

The single source of truth for runtime configuration. Every secret and tunable
that appears in `.env.example` lives here as a typed field. Domain-specific
config bubbles up through this object.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- application ----
    app_env: Literal["dev", "staging", "prod"] = "dev"
    app_host: str = "0.0.0.0"  # noqa: S104 — server binds to all interfaces by design
    app_port: int = 8000
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    log_level: Literal["debug", "info", "warn", "error"] = "info"

    # ---- database ----
    database_url: str = (
        "postgresql+asyncpg://livepeer_open_clearinghouse:"
        "livepeer_open_clearinghouse-dev-password@localhost:5432/"
        "livepeer_open_clearinghouse"
    )

    # ---- secrets (see docs/SECURITY.md) ----
    api_key_hash_pepper: SecretStr = SecretStr("dev-pepper-CHANGE-FOR-PROD")
    session_secret: SecretStr = SecretStr("dev-session-secret-CHANGE-FOR-PROD")
    metrics_token: SecretStr = SecretStr("dev-metrics-token-CHANGE-FOR-PROD")
    admin_bootstrap_token: SecretStr | None = None

    # ---- OAuth ----
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None

    # ---- email (Resend) ----
    email_provider: Literal["auto", "null", "resend"] = "auto"
    resend_api_key: SecretStr | None = None
    resend_api_url: str | None = (
        None  # override https://api.resend.com (regional API, on-prem proxy, etc.)
    )
    # Standard Webhooks signing secret for inbound POST /v1/webhooks/resend.
    # Configured in Resend's dashboard per-webhook; expected form is
    # `whsec_<base64>`. When unset, the webhook endpoint returns 503.
    resend_webhook_secret: SecretStr | None = None
    email_from_address: str = "no-reply@livepeer-open-clearinghouse.local"
    email_from_name: str = "Livepeer Open Clearinghouse"

    # ---- daemon UDS paths ----
    payment_daemon_mode: Literal["mock", "grpc"] = "mock"
    payment_daemon_socket: str = "/var/run/livepeer/payer-daemon.sock"
    registry_daemon_mode: Literal["mock", "grpc"] = "mock"
    registry_daemon_socket: str = "/var/run/livepeer/service-registry.sock"
    registry_cache_ttl_seconds: int = Field(default=60, ge=0)

    # ---- billing defaults ----
    default_initial_credit_wei: int = Field(default=0, ge=0)
    default_spend_period_seconds: int = Field(default=86_400, ge=60)
    default_spend_period_cap_wei: int = Field(default=0, ge=0)
    auto_replenish_increment_wei: int = Field(default=0, ge=0)
    # Proactive auto-replenish: how often the scheduler scans for users
    # whose balance has dropped below `auto_replenish_threshold_wei`.
    # Set to 0 to disable the job entirely (replenish becomes operator-
    # topup-only). Default 300s mirrors the deposit-snapshot cadence.
    auto_replenish_check_interval_seconds: int = Field(default=300, ge=0)

    # ---- ticket-mint reliability ----
    idempotency_inflight_timeout_seconds: int = Field(default=60, ge=5)
    idempotency_retention_seconds: int = Field(default=86_400, ge=60)
    job_reconciliation_interval_seconds: int = Field(default=60, ge=0)
    session_reconciliation_interval_seconds: int = Field(default=60, ge=0)

    # ---- per-IP rate limits (in-process token bucket) ----
    rl_login_capacity: int = Field(default=10, ge=0)
    rl_login_refill_per_minute: int = Field(default=10, ge=0)
    rl_signup_capacity: int = Field(default=5, ge=0)
    rl_signup_refill_per_minute: int = Field(default=5, ge=0)
    rl_password_reset_capacity: int = Field(default=3, ge=0)
    rl_password_reset_refill_per_minute: int = Field(default=3, ge=0)

    # ---- telemetry (exec-plan 002 §"SDK telemetry") ----
    telemetry_raw_retention_days: int = Field(default=30, ge=0)
    telemetry_retention_janitor_interval_seconds: int = Field(default=3600, ge=60)
    # Per-API-key ingest cap, events/sec. Burst capacity matches the
    # per-second rate so a healthy SDK draining its batch buffer doesn't
    # get throttled.
    telemetry_ingest_rate_per_key: int = Field(default=10_000, ge=0)
    # Per-API-key cap on GET /v1/telemetry/events, requests/min. Separate
    # from ingest because read patterns are bursty (dashboards refresh)
    # and a tighter rate is appropriate.
    telemetry_query_rate_per_key_per_minute: int = Field(default=100, ge=0)
    # Hard ceiling on events returned per query page. Pagination is
    # cursor-based; clients walk pages to consume bigger windows.
    telemetry_query_max_page_size: int = Field(default=500, ge=1, le=5000)
    # Identifier for this LOC replica. Stamped on every telemetry row
    # so admin can attribute ingest spikes to a specific node. Defaults
    # to the container hostname when unset.
    ingest_node_id: str | None = None
    # Operator Ed25519 signing-key seed, base64-encoded (32 raw bytes →
    # 44 chars). When unset, the SDK manifest is served unsigned — fine
    # for dev; in prod set this in the operator's secret store so SDKs
    # can verify against ``GET /v1/sdk/manifest/pubkey``.
    sdk_manifest_signing_key: SecretStr | None = None
    # HMAC seed used to derive per-user webhook signing secrets. Each
    # customer's signing secret = HMAC_SHA256(seed, user_id). Rotating
    # the seed invalidates every customer's webhook secret at once
    # (v1 limitation). Unset → webhook channel disabled.
    webhook_signing_seed: SecretStr | None = None
    # Outbound webhook timing knobs.
    webhook_send_timeout_seconds: float = Field(default=10.0, gt=0)
    webhook_send_max_retries: int = Field(default=3, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()
