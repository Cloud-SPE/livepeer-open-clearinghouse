"""OAuth registry construction (Google + GitHub) using authlib.

A provider is only registered when both its CLIENT_ID and CLIENT_SECRET
are configured. Callers should check ``is_enabled(provider)`` before
invoking the flow; the runtime endpoints return 404 for disabled
providers.

Authlib requires Starlette's ``SessionMiddleware`` on the app (see
main.py) so it can stash CSRF state between the redirect and callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from authlib.integrations.starlette_client import OAuth

from pymthouse.settings import Settings, get_settings

PROVIDERS = ("google", "github")


@dataclass(frozen=True, slots=True)
class OAuthUserInfo:
    """Normalized user info pulled from a provider after the code exchange."""

    provider: str
    provider_user_id: str
    email: str
    email_verified: bool


_REGISTERED: set[str] = set()


def _has_secret(secret: object) -> bool:
    """True iff a SecretStr is set to a non-empty value.

    Pydantic Settings reads env vars as strings, so an env line like
    ``GOOGLE_OAUTH_CLIENT_SECRET=`` (present but empty) parses to a
    SecretStr wrapping ``""`` — not None. Treat both as "not configured."
    """
    if secret is None:
        return False
    try:
        return bool(secret.get_secret_value())  # type: ignore[attr-defined]
    except AttributeError:
        return bool(secret)


def _maybe_register_google(oauth: OAuth, settings: Settings) -> bool:
    if not settings.google_oauth_client_id or not _has_secret(
        settings.google_oauth_client_secret
    ):
        return False
    oauth.register(
        name="google",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    _REGISTERED.add("google")
    return True


def _maybe_register_github(oauth: OAuth, settings: Settings) -> bool:
    if not settings.github_oauth_client_id or not _has_secret(
        settings.github_oauth_client_secret
    ):
        return False
    oauth.register(
        name="github",
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret.get_secret_value(),
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    )
    _REGISTERED.add("github")
    return True


@lru_cache(maxsize=1)
def get_oauth() -> OAuth:
    """Process-wide OAuth registry."""
    oauth = OAuth()
    cfg = get_settings()
    _REGISTERED.clear()
    _maybe_register_google(oauth, cfg)
    _maybe_register_github(oauth, cfg)
    return oauth


def is_enabled(provider: str) -> bool:
    """True iff `provider` was successfully registered with real credentials.

    Authlib's ``create_client`` will *manufacture* a client even for a
    name that was never ``register()``'d (it falls back to app-config /
    framework integration lookups), so we can't use it as a proxy for
    "is this configured?". Instead we track the set of names that
    ``_maybe_register_*`` actually wrote credentials for.
    """
    if provider not in PROVIDERS:
        return False
    get_oauth()  # ensure registry is built
    return provider in _REGISTERED
