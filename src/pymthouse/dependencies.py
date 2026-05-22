"""FastAPI dependency functions — the wiring layer.

Composes providers + domain services into Annotated dependencies that
runtime routers consume. This is the only module that imports from both
`providers/*` and `domains/*`.

Keep this file pure plumbing. Business logic lives in domain `service.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from pymthouse.domains.accounts import service as accounts_service
from pymthouse.domains.accounts.repo import User
from pymthouse.domains.admin import service as admin_service
from pymthouse.domains.admin.repo import Operator
from pymthouse.domains.api_keys import service as api_keys_service
from pymthouse.domains.api_keys.repo import ApiKey
from pymthouse.providers.auth import session as session_helper
from pymthouse.providers.clock import Clock, DefaultClock
from pymthouse.providers.db import session_dependency
from pymthouse.providers.email import EmailProvider, make_provider as make_email_provider
from pymthouse.providers.registry_daemon import (
    MockRegistryClient,
    RegistryClient,
)
from pymthouse.settings import Settings, get_settings

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _default_clock() -> Clock:
    return DefaultClock()


@lru_cache(maxsize=1)
def _default_email() -> EmailProvider:
    return make_email_provider(get_settings())


@lru_cache(maxsize=1)
def _default_registry() -> RegistryClient:
    return MockRegistryClient()


# ---------------------------------------------------------------------------
# FastAPI dependency callables
# ---------------------------------------------------------------------------


def get_clock() -> Clock:
    return _default_clock()


def get_email() -> EmailProvider:
    return _default_email()


def get_registry() -> RegistryClient:
    return _default_registry()


def get_settings_dep() -> Settings:
    return get_settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    async for s in session_dependency():
        yield s


# ---------------------------------------------------------------------------
# Identity dependencies
# ---------------------------------------------------------------------------


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
EmailDep = Annotated[EmailProvider, Depends(get_email)]
RegistryDep = Annotated[RegistryClient, Depends(get_registry)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _unauthorized(detail: str = "unauthorized") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def get_current_api_key_and_user(
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[ApiKey, User]:
    """Resolve the caller from `X-API-Key` or `Authorization: Bearer`.

    Used by app-dev-facing endpoints (discovery, payments, usage).
    """
    raw = x_api_key
    if raw is None and authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            raw = token
    if not raw:
        raise _unauthorized("missing api key")

    result = await api_keys_service.validate_raw_key(
        session,
        raw_key=raw,
        pepper=settings.api_key_hash_pepper.get_secret_value(),
        clock=clock,
    )
    if result is None:
        raise _unauthorized("invalid api key")
    return result


async def get_current_user_from_api_key(
    pair: Annotated[tuple[ApiKey, User], Depends(get_current_api_key_and_user)],
) -> User:
    return pair[1]


async def get_session_user(
    request: Request,  # noqa: ARG001 — kept for symmetry/future use
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    cookie: Annotated[
        str | None, Cookie(alias=session_helper.SESSION_COOKIE_NAME)
    ] = None,
) -> User:
    """Resolve the user behind a portal session cookie."""
    if cookie is None:
        raise _unauthorized("missing session")
    serializer = session_helper.make_serializer(
        settings.session_secret.get_secret_value()
    )
    max_age = int(accounts_service.SESSION_TTL.total_seconds())
    raw_token = session_helper.unseal(serializer, cookie, max_age_seconds=max_age)
    if raw_token is None:
        raise _unauthorized("invalid session")
    user = await accounts_service.resolve_session(
        session, raw_token=raw_token, clock=clock
    )
    if user is None:
        raise _unauthorized("invalid session")
    return user


async def get_current_operator(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Operator:
    """Resolve an operator from `Authorization: Bearer <token>`."""
    if authorization is None:
        raise _unauthorized("missing operator token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized("invalid operator token")
    op = await admin_service.authenticate_operator(session, bearer_token=token)
    if op is None:
        raise _unauthorized("invalid operator token")
    return op


CurrentApiKeyDep = Annotated[
    tuple[ApiKey, User], Depends(get_current_api_key_and_user)
]
CurrentUserFromApiKeyDep = Annotated[User, Depends(get_current_user_from_api_key)]
SessionUserDep = Annotated[User, Depends(get_session_user)]
CurrentOperatorDep = Annotated[Operator, Depends(get_current_operator)]
