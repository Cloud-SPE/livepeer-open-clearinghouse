"""Pydantic models for the accounts domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    """Inbound: ``POST /v1/accounts/signup``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


class VerifyEmailRequest(BaseModel):
    """Inbound: ``POST /v1/accounts/verify-email``."""

    token: str = Field(min_length=10)


class LoginRequest(BaseModel):
    """Inbound: ``POST /v1/auth/login``."""

    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """Outbound: the public view of a user."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    email_verified_at: datetime | None
    approved: bool
    created_at: datetime


class SignupResponse(BaseModel):
    """Outbound: ``POST /v1/accounts/signup`` success."""

    user: UserResponse
    verification_required: bool = True


class LoginResponse(BaseModel):
    """Outbound: ``POST /v1/auth/login`` success.

    The session token is delivered via cookie; the body just confirms.
    """

    user: UserResponse


class RequestPasswordResetRequest(BaseModel):
    """Inbound: ``POST /v1/auth/password-reset/request``."""

    email: EmailStr


class ResendVerificationRequest(BaseModel):
    """Inbound: ``POST /v1/auth/resend-verification``."""

    email: EmailStr


class ConfirmPasswordResetRequest(BaseModel):
    """Inbound: ``POST /v1/auth/password-reset/confirm``."""

    token: str = Field(min_length=10)
    new_password: str = Field(min_length=12, max_length=256)
