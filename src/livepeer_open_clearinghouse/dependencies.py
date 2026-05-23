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

from livepeer_open_clearinghouse.domains.accounts import service as accounts_service
from livepeer_open_clearinghouse.domains.accounts.repo import User
from livepeer_open_clearinghouse.domains.admin import service as admin_service
from livepeer_open_clearinghouse.domains.admin.repo import Operator
from livepeer_open_clearinghouse.domains.api_keys import service as api_keys_service
from livepeer_open_clearinghouse.domains.api_keys.repo import ApiKey
from livepeer_open_clearinghouse.providers.auth import session as session_helper
from livepeer_open_clearinghouse.providers.clock import Clock, DefaultClock
from livepeer_open_clearinghouse.providers.db import session_dependency
from livepeer_open_clearinghouse.providers.email import (
    EmailProvider,
)
from livepeer_open_clearinghouse.providers.email import (
    make_provider as make_email_provider,
)
from livepeer_open_clearinghouse.providers.payment_daemon import (
    GrpcPaymentDaemonClient,
    MockPaymentDaemonClient,
    PaymentDaemonClient,
)
from livepeer_open_clearinghouse.providers.ratelimit import RateLimiter
from livepeer_open_clearinghouse.providers.registry_daemon import (
    CachingRegistryClient,
    GrpcRegistryClient,
    MockRegistryClient,
    RegistryClient,
)
from livepeer_open_clearinghouse.settings import Settings, get_settings

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
    cfg = get_settings()
    inner: RegistryClient
    if cfg.registry_daemon_mode == "grpc":
        inner = GrpcRegistryClient(cfg.registry_daemon_socket)
    else:
        inner = MockRegistryClient()
    if cfg.registry_cache_ttl_seconds > 0:
        return CachingRegistryClient(inner, ttl_seconds=cfg.registry_cache_ttl_seconds)
    return inner


@lru_cache(maxsize=1)
def _default_payment_daemon() -> PaymentDaemonClient:
    cfg = get_settings()
    if cfg.payment_daemon_mode == "grpc":
        return GrpcPaymentDaemonClient(cfg.payment_daemon_socket)
    return MockPaymentDaemonClient()


@lru_cache(maxsize=1)
def _default_rate_limiter() -> RateLimiter:
    return RateLimiter()


# ---------------------------------------------------------------------------
# FastAPI dependency callables
# ---------------------------------------------------------------------------


def get_clock() -> Clock:
    return _default_clock()


def get_email() -> EmailProvider:
    return _default_email()


def get_registry() -> RegistryClient:
    return _default_registry()


def get_payment_daemon() -> PaymentDaemonClient:
    return _default_payment_daemon()


def get_rate_limiter() -> RateLimiter:
    return _default_rate_limiter()


def get_settings_dep() -> Settings:
    return get_settings()


async def get_session() -> AsyncIterator[AsyncSession]:
    async for s in session_dependency():
        yield s


# ---------------------------------------------------------------------------
# Rate-limit dependency factory
# ---------------------------------------------------------------------------


def rate_limit(*, route: str, capacity_attr: str, refill_attr: str):  # type: ignore[no-untyped-def]
    """Build a FastAPI dependency that throttles `route` per-IP.

    `capacity_attr` and `refill_attr` are the names of the corresponding
    fields on Settings (so each route reads its own knobs from env).
    A capacity of 0 disables the limiter for that route.
    """
    from fastapi import HTTPException, status  # noqa: PLC0415

    async def _dep(
        request: Request,
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
        settings: Annotated[Settings, Depends(get_settings_dep)],
    ) -> None:
        capacity = int(getattr(settings, capacity_attr))
        refill = int(getattr(settings, refill_attr))
        ip = request.client.host if request.client is not None else "0.0.0.0"
        allowed, retry_after = await limiter.acquire(
            route=route,
            key=ip,
            capacity=capacity,
            refill_per_minute=refill,
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate_limited",
                headers={"Retry-After": str(retry_after)},
            )

    return _dep


# ---------------------------------------------------------------------------
# Identity dependencies
# ---------------------------------------------------------------------------


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
EmailDep = Annotated[EmailProvider, Depends(get_email)]
RegistryDep = Annotated[RegistryClient, Depends(get_registry)]
PaymentDaemonDep = Annotated[PaymentDaemonClient, Depends(get_payment_daemon)]
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
    request: Request,
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    cookie: Annotated[str | None, Cookie(alias=session_helper.SESSION_COOKIE_NAME)] = None,
) -> User:
    """Resolve the user behind a portal session cookie."""
    if cookie is None:
        raise _unauthorized("missing session")
    serializer = session_helper.make_serializer(settings.session_secret.get_secret_value())
    max_age = int(accounts_service.SESSION_TTL.total_seconds())
    raw_token = session_helper.unseal(serializer, cookie, max_age_seconds=max_age)
    if raw_token is None:
        raise _unauthorized("invalid session")
    user = await accounts_service.resolve_session(session, raw_token=raw_token, clock=clock)
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


CurrentApiKeyDep = Annotated[tuple[ApiKey, User], Depends(get_current_api_key_and_user)]
CurrentUserFromApiKeyDep = Annotated[User, Depends(get_current_user_from_api_key)]


async def get_authed_user(
    request: Request,
    session: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    cookie: Annotated[str | None, Cookie(alias=session_helper.SESSION_COOKIE_NAME)] = None,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """Resolve the user via *either* the session cookie *or* X-API-Key.

    Used by read-only endpoints (currently discovery) that should be
    callable by:
      - server-side SDK clients holding a `pymth_live_...` API key, and
      - the portal SPA, which is cookie-session-authenticated.

    API-key takes precedence when both are present (server-side calls
    usually don't also carry the portal cookie). Returns the matching
    User. Raises 401 if neither resolves.
    """
    if api_key is not None and api_key.strip():
        pair = await api_keys_service.validate_raw_key(
            session,
            raw_key=api_key,
            pepper=settings.api_key_hash_pepper.get_secret_value(),
            clock=clock,
        )
        if pair is None:
            raise _unauthorized("invalid api key")
        return pair[1]
    if cookie is not None:
        serializer = session_helper.make_serializer(settings.session_secret.get_secret_value())
        max_age = int(accounts_service.SESSION_TTL.total_seconds())
        raw_token = session_helper.unseal(serializer, cookie, max_age_seconds=max_age)
        if raw_token is not None:
            user = await accounts_service.resolve_session(session, raw_token=raw_token, clock=clock)
            if user is not None:
                return user
        raise _unauthorized("invalid session")
    raise _unauthorized("missing credentials")


AuthedUserDep = Annotated[User, Depends(get_authed_user)]
SessionUserDep = Annotated[User, Depends(get_session_user)]
CurrentOperatorDep = Annotated[Operator, Depends(get_current_operator)]


async def get_owner_operator(
    operator: Annotated[Operator, Depends(get_current_operator)],
) -> Operator:
    """Like ``get_current_operator``, but 403 unless role == ``owner``.

    Used by the operator-management endpoints under
    ``/v1/admin/operators``. ``member`` operators get a clean
    permission-denied instead of having to discover the rule by trial
    and error.
    """
    if operator.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="operator_role_required:owner",
        )
    return operator


OwnerOperatorDep = Annotated[Operator, Depends(get_owner_operator)]
