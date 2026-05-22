"""FastAPI routes for the accounts domain."""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from pymthouse.dependencies import (
    ClockDep,
    EmailDep,
    SessionDep,
    SessionUserDep,
    SettingsDep,
)
from pymthouse.domains.accounts import oauth as oauth_service
from pymthouse.domains.accounts import service
from pymthouse.domains.accounts.repo import User, UserSession
from pymthouse.domains.accounts.types import (
    ConfirmPasswordResetRequest,
    LoginRequest,
    LoginResponse,
    RequestPasswordResetRequest,
    SignupRequest,
    SignupResponse,
    UserResponse,
    VerifyEmailRequest,
)
from pymthouse.providers.auth import session as session_helper
from pymthouse.providers.oauth import is_enabled as oauth_is_enabled
from pymthouse.providers.oauth import get_oauth

router = APIRouter(tags=["accounts"])


def _user_response(user: User, *, approved: bool) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        email_verified_at=user.email_verified_at,
        approved=approved,
        created_at=user.created_at,
    )


@router.post(
    "/v1/accounts/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup_endpoint(
    body: SignupRequest,
    db: SessionDep,
    clock: ClockDep,
    email: EmailDep,
    settings: SettingsDep,
) -> SignupResponse:
    try:
        user = await service.signup(
            db,
            email=str(body.email),
            password=body.password,
            clock=clock,
            email_provider=email,
            public_base_url=str(settings.public_base_url),
        )
    except service.EmailAlreadyRegistered as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return SignupResponse(user=_user_response(user, approved=False))


@router.post("/v1/accounts/verify-email", response_model=UserResponse)
async def verify_email_endpoint(
    body: VerifyEmailRequest,
    db: SessionDep,
    clock: ClockDep,
) -> UserResponse:
    try:
        user = await service.verify_email(db, token=body.token, clock=clock)
    except service.InvalidToken as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    approved = await service.is_approved(db, user.id)
    return _user_response(user, approved=approved)


@router.post(
    "/v1/auth/password-reset/request",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset_endpoint(
    body: RequestPasswordResetRequest,
    db: SessionDep,
    clock: ClockDep,
    email: EmailDep,
    settings: SettingsDep,
) -> Response:
    """Always returns 202, regardless of whether the email is registered.

    The reset email is sent only when a matching user exists; otherwise
    the endpoint is a silent no-op. This deliberate symmetry stops the
    endpoint from being used to enumerate registered addresses.
    """
    await service.request_password_reset(
        db,
        email=str(body.email),
        clock=clock,
        email_provider=email,
        public_base_url=str(settings.public_base_url),
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/v1/auth/password-reset/confirm", response_model=UserResponse)
async def confirm_password_reset_endpoint(
    body: ConfirmPasswordResetRequest,
    db: SessionDep,
    clock: ClockDep,
) -> UserResponse:
    try:
        user = await service.reset_password(
            db, token=body.token, new_password=body.new_password, clock=clock
        )
    except service.InvalidToken as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    approved = await service.is_approved(db, user.id)
    return _user_response(user, approved=approved)


@router.post("/v1/auth/login", response_model=LoginResponse)
async def login_endpoint(
    body: LoginRequest,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    response: Response,
) -> LoginResponse:
    try:
        user, raw_token = await service.login(
            db, email=str(body.email), password=body.password, clock=clock
        )
    except service.InvalidCredentials as exc:
        raise HTTPException(status_code=401, detail=exc.code) from exc
    except service.EmailNotVerified as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc

    serializer = session_helper.make_serializer(
        settings.session_secret.get_secret_value()
    )
    sealed = session_helper.seal(serializer, raw_token)
    response.set_cookie(
        key=session_helper.SESSION_COOKIE_NAME,
        value=sealed,
        max_age=int(service.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.app_env != "dev",
        samesite="lax",
        path="/",
    )
    approved = await service.is_approved(db, user.id)
    return LoginResponse(user=_user_response(user, approved=approved))


@router.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    response: Response,
    pymthouse_session: Annotated[
        str | None, Cookie(alias=session_helper.SESSION_COOKIE_NAME)
    ] = None,
) -> Response:
    """Clear the session cookie and revoke its DB row if valid.

    Unauthenticated — calling with a stale cookie just clears it.
    """
    if pymthouse_session:
        serializer = session_helper.make_serializer(
            settings.session_secret.get_secret_value()
        )
        max_age = int(service.SESSION_TTL.total_seconds())
        raw = session_helper.unseal(
            serializer, pymthouse_session, max_age_seconds=max_age
        )
        if raw:
            await service.revoke_session(db, raw_token=raw, clock=clock)
    response.delete_cookie(session_helper.SESSION_COOKIE_NAME, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v1/accounts/me", response_model=UserResponse)
async def me_endpoint(
    user: SessionUserDep,
    db: SessionDep,
) -> UserResponse:
    approved = await service.is_approved(db, user.id)
    return _user_response(user, approved=approved)


# ---------------------------------------------------------------------------
# OAuth (Google + GitHub)
# ---------------------------------------------------------------------------


@router.get("/v1/auth/oauth/providers", tags=["oauth"])
async def oauth_providers_endpoint() -> dict[str, list[str]]:
    """Public — which OAuth providers the portal should render buttons for."""
    return {
        "enabled": [p for p in ("google", "github") if oauth_is_enabled(p)]
    }


def _redirect_uri(request: Request, provider: str) -> str:
    return str(
        request.url_for("oauth_callback_endpoint", provider=provider)
    )


@router.get("/v1/auth/oauth/{provider}/login", tags=["oauth"])
async def oauth_login_endpoint(
    provider: str,
    request: Request,
) -> Response:
    if not oauth_is_enabled(provider):
        raise HTTPException(status_code=404, detail="oauth_provider_disabled")
    client = get_oauth().create_client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail="oauth_provider_disabled")
    redirect_uri = _redirect_uri(request, provider)
    return await client.authorize_redirect(request, redirect_uri)


async def _github_userinfo(token: dict) -> tuple[str, str, bool]:
    """Fetch GitHub's profile + primary verified email.

    GitHub's OAuth profile endpoint returns the public profile; the
    email scope is needed to read /user/emails and pick the verified
    primary address.
    """
    access_token = token.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="oauth_no_token")
    headers = {
        "Authorization": f"token {access_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        profile_resp = await client.get("https://api.github.com/user", headers=headers)
        profile_resp.raise_for_status()
        profile = profile_resp.json()
        emails_resp = await client.get(
            "https://api.github.com/user/emails", headers=headers
        )
        emails_resp.raise_for_status()
        emails = emails_resp.json() or []
    primary_verified = next(
        (e for e in emails if e.get("primary") and e.get("verified")),
        None,
    )
    if primary_verified is None:
        # Refuse: we won't create an account from an unverified address.
        raise HTTPException(status_code=400, detail="oauth_email_unverified")
    return (
        str(profile["id"]),
        primary_verified["email"],
        True,
    )


@router.get("/v1/auth/oauth/{provider}/callback", name="oauth_callback_endpoint", tags=["oauth"])
async def oauth_callback_endpoint(
    provider: str,
    request: Request,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> RedirectResponse:
    if not oauth_is_enabled(provider):
        raise HTTPException(status_code=404, detail="oauth_provider_disabled")
    client = get_oauth().create_client(provider)
    if client is None:
        raise HTTPException(status_code=404, detail="oauth_provider_disabled")

    try:
        token = await client.authorize_access_token(request)
    except Exception as exc:  # noqa: BLE001 — authlib exceptions are not stable
        raise HTTPException(status_code=400, detail="oauth_exchange_failed") from exc

    if provider == "google":
        userinfo = token.get("userinfo") or {}
        provider_user_id = userinfo.get("sub")
        email = userinfo.get("email")
        email_verified = bool(userinfo.get("email_verified", False))
        if not provider_user_id or not email:
            raise HTTPException(status_code=400, detail="oauth_no_profile")
    elif provider == "github":
        provider_user_id, email, email_verified = await _github_userinfo(token)
    else:
        raise HTTPException(status_code=404, detail="oauth_provider_disabled")

    try:
        user = await oauth_service.find_or_link_user(
            db,
            provider=provider,
            provider_user_id=str(provider_user_id),
            email=email,
            email_verified=email_verified,
            clock=clock,
        )
    except oauth_service.UnverifiedProviderEmail as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc

    # Issue a session cookie just like the password-login path does.
    raw_token = session_helper.generate_token()
    token_hash = session_helper.hash_token(raw_token)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=clock.now() + service.SESSION_TTL,
        )
    )
    await db.flush()

    serializer = session_helper.make_serializer(
        settings.session_secret.get_secret_value()
    )
    sealed = session_helper.seal(serializer, raw_token)

    response = RedirectResponse(url="/portal/#/", status_code=303)
    response.set_cookie(
        key=session_helper.SESSION_COOKIE_NAME,
        value=sealed,
        max_age=int(service.SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.app_env != "dev",
        samesite="lax",
        path="/",
    )
    return response
