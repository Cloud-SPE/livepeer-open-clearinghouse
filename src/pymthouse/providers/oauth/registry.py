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


def _maybe_register_google(oauth: OAuth, settings: Settings) -> bool:
    if settings.google_oauth_client_id is None or settings.google_oauth_client_secret is None:
        return False
    oauth.register(
        name="google",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret.get_secret_value(),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return True


def _maybe_register_github(oauth: OAuth, settings: Settings) -> bool:
    if settings.github_oauth_client_id is None or settings.github_oauth_client_secret is None:
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
    return True


@lru_cache(maxsize=1)
def get_oauth() -> OAuth:
    """Process-wide OAuth registry."""
    oauth = OAuth()
    cfg = get_settings()
    _maybe_register_google(oauth, cfg)
    _maybe_register_github(oauth, cfg)
    return oauth


def is_enabled(provider: str) -> bool:
    """True iff `provider` was successfully registered (client id+secret set)."""
    if provider not in PROVIDERS:
        return False
    try:
        return get_oauth().create_client(provider) is not None
    except Exception:  # noqa: BLE001 — authlib raises generic types on missing config
        return False
