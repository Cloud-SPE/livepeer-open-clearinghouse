"""FastAPI routes for the accounts domain."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status

from pymthouse.dependencies import (
    ClockDep,
    EmailDep,
    SessionDep,
    SessionUserDep,
    SettingsDep,
)
from pymthouse.domains.accounts import service
from pymthouse.domains.accounts.repo import User
from pymthouse.domains.accounts.types import (
    LoginRequest,
    LoginResponse,
    SignupRequest,
    SignupResponse,
    UserResponse,
    VerifyEmailRequest,
)
from pymthouse.providers.auth import session as session_helper

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
