"""FastAPI routes for the api_keys domain (portal surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from pymthouse.dependencies import (
    ClockDep,
    SessionDep,
    SessionUserDep,
    SettingsDep,
)
from pymthouse.domains.api_keys import service
from pymthouse.domains.api_keys.types import (
    ApiKeyList,
    ApiKeyView,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
)

router = APIRouter(tags=["api_keys"])


@router.post(
    "/v1/accounts/me/api-keys",
    response_model=CreateApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key_endpoint(
    body: CreateApiKeyRequest,
    user: SessionUserDep,
    db: SessionDep,
    settings: SettingsDep,
) -> CreateApiKeyResponse:
    row, raw_key = await service.create(
        db,
        user_id=user.id,
        label=body.label,
        pepper=settings.api_key_hash_pepper.get_secret_value(),
    )
    return CreateApiKeyResponse(
        key=ApiKeyView.model_validate(row),
        raw_key=raw_key,
    )


@router.get("/v1/accounts/me/api-keys", response_model=ApiKeyList)
async def list_api_keys_endpoint(
    user: SessionUserDep,
    db: SessionDep,
) -> ApiKeyList:
    rows = await service.list_for_user(db, user_id=user.id)
    return ApiKeyList(items=[ApiKeyView.model_validate(r) for r in rows])


@router.delete(
    "/v1/accounts/me/api-keys/{api_key_id}",
    response_model=ApiKeyView,
)
async def revoke_api_key_endpoint(
    api_key_id: uuid.UUID,
    user: SessionUserDep,
    db: SessionDep,
    clock: ClockDep,
) -> ApiKeyView:
    try:
        row = await service.revoke(
            db, user_id=user.id, api_key_id=api_key_id, clock=clock
        )
    except service.ApiKeyNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    return ApiKeyView.model_validate(row)
