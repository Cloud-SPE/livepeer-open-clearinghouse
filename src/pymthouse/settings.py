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
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    log_level: Literal["debug", "info", "warn", "error"] = "info"

    # ---- database ----
    database_url: str = "postgresql+asyncpg://pymthouse:pymthouse-dev@localhost:5432/pymthouse"

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
    email_from_address: str = "no-reply@pymthouse.local"
    email_from_name: str = "PymtHouse"

    # ---- daemon UDS paths ----
    payment_daemon_mode: Literal["mock", "grpc"] = "mock"
    payment_daemon_socket: str = "/var/run/livepeer/payer-daemon.sock"
    registry_daemon_mode: Literal["mock", "grpc"] = "mock"
    registry_daemon_socket: str = "/var/run/livepeer/service-registry.sock"

    # ---- billing defaults ----
    default_initial_credit_wei: int = Field(default=0, ge=0)
    default_spend_period_seconds: int = Field(default=86_400, ge=60)
    default_spend_period_cap_wei: int = Field(default=0, ge=0)
    auto_replenish_increment_wei: int = Field(default=0, ge=0)

    # ---- ticket-mint reliability ----
    idempotency_inflight_timeout_seconds: int = Field(default=60, ge=5)

    # ---- per-IP rate limits (in-process token bucket) ----
    rl_login_capacity: int = Field(default=10, ge=0)
    rl_login_refill_per_minute: int = Field(default=10, ge=0)
    rl_signup_capacity: int = Field(default=5, ge=0)
    rl_signup_refill_per_minute: int = Field(default=5, ge=0)
    rl_password_reset_capacity: int = Field(default=3, ge=0)
    rl_password_reset_refill_per_minute: int = Field(default=3, ge=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings singleton."""
    return Settings()
