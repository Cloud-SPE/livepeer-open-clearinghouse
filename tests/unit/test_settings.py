from __future__ import annotations

import pytest
from pydantic import ValidationError

from livepeer_open_clearinghouse.settings import Settings


def _production_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "app_env": "prod",
        "public_base_url": "https://loc.example.com",
        "database_url": "postgresql+asyncpg://loc:secret@postgres.internal/loc",
        "api_key_hash_pepper": "p" * 32,
        "session_secret": "s" * 32,
        "metrics_token": "m" * 32,
        "payment_daemon_mode": "grpc",
        "registry_daemon_mode": "grpc",
        "email_provider": "resend",
        "resend_api_key": "nusend-production-key",
        "email_from_address": "info@loc.example.com",
        "sdk_manifest_signing_key": "c2lnbmluZy1rZXktZm9yLXByb2R1Y3Rpb24=",
        "admin_bootstrap_token": None,
    }
    values.update(overrides)
    return values


@pytest.mark.unit
def test_production_settings_accept_hardened_values() -> None:
    settings = Settings(**_production_settings())  # type: ignore[arg-type]

    assert settings.app_env == "prod"
    assert settings.public_base_url.scheme == "https"


@pytest.mark.unit
def test_production_settings_reject_development_defaults() -> None:
    with pytest.raises(ValidationError, match="unsafe production configuration") as raised:
        Settings(
            app_env="prod",
            public_base_url="http://localhost:8000",
            database_url=(
                "postgresql+asyncpg://livepeer_open_clearinghouse:"
                "livepeer_open_clearinghouse-dev-password@localhost/loc"
            ),
            api_key_hash_pepper="dev-pepper-CHANGE-FOR-PROD",
            session_secret="dev-session-secret-CHANGE-FOR-PROD",
            metrics_token="dev-metrics-token-CHANGE-FOR-PROD",
            payment_daemon_mode="mock",
            registry_daemon_mode="mock",
            email_provider="auto",
            resend_api_key=None,
            email_from_address="no-reply@livepeer-open-clearinghouse.local",
            sdk_manifest_signing_key=None,
            admin_bootstrap_token=None,
        )

    message = str(raised.value)
    assert "PUBLIC_BASE_URL must use https" in message
    assert "PAYMENT_DAEMON_MODE must be grpc" in message
    assert "REGISTRY_DAEMON_MODE must be grpc" in message
    assert "DATABASE_URL must not use the development password" in message
    assert "EMAIL_PROVIDER must be resend with RESEND_API_KEY" in message
    assert "EMAIL_FROM_ADDRESS must not use a local development domain" in message
    assert "SDK_MANIFEST_SIGNING_KEY is required" in message
    assert "API_KEY_HASH_PEPPER" in message
    assert "SESSION_SECRET" in message
    assert "METRICS_TOKEN" in message
