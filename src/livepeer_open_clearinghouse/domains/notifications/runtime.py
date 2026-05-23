"""FastAPI routes for the notifications domain.

Two surfaces:
  * ``POST /v1/webhooks/resend`` — the inbound webhook. No API-key /
    session auth; the entire trust boundary is the Standard Webhooks
    HMAC signature.
  * ``GET /v1/admin/email/events`` — operator visibility into the
    event log. Uses the existing operator-bearer auth.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status

from livepeer_open_clearinghouse.dependencies import (
    ClockDep,
    CurrentOperatorDep,
    SessionDep,
    SettingsDep,
)
from livepeer_open_clearinghouse.domains.notifications import service
from livepeer_open_clearinghouse.domains.notifications.types import (
    EmailEventList,
    EmailEventView,
    ResendWebhookEvent,
    WebhookAcceptedResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["notifications"])


@router.post(
    "/v1/webhooks/resend",
    response_model=WebhookAcceptedResponse,
)
async def resend_webhook(
    request: Request,
    db: SessionDep,
    clock: ClockDep,
    settings: SettingsDep,
    webhook_id: Annotated[str | None, Header(alias="webhook-id")] = None,
    webhook_timestamp: Annotated[str | None, Header(alias="webhook-timestamp")] = None,
    webhook_signature: Annotated[str | None, Header(alias="webhook-signature")] = None,
) -> WebhookAcceptedResponse:
    """Accept and persist a Resend webhook callback.

    Auth: HMAC-SHA256 over ``{webhook-id}.{webhook-timestamp}.{body}``
    against ``RESEND_WEBHOOK_SECRET``. No other auth.
    """
    secret_obj = settings.resend_webhook_secret
    if secret_obj is None or not secret_obj.get_secret_value():
        # Endpoint exists but is not configured — fail closed.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="resend_webhook_not_configured",
        )

    body = await request.body()
    try:
        service.verify_signature(
            secret=secret_obj.get_secret_value(),
            webhook_id=webhook_id,
            webhook_timestamp=webhook_timestamp,
            webhook_signature=webhook_signature,
            body=body,
            clock=clock,
        )
    except service.WebhookSignatureError as exc:
        log.warning("resend.webhook.signature_rejected", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    try:
        event = ResendWebhookEvent.model_validate_json(body)
    except Exception as exc:  # noqa: BLE001 — fall through to 400
        log.warning("resend.webhook.bad_payload", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="bad_payload"
        ) from exc

    # webhook_id is the dedup key. Already-verified above to be non-None.
    assert webhook_id is not None
    row, was_new = await service.ingest_event(
        db, event=event, provider_event_id=webhook_id, clock=clock
    )
    log.info(
        "resend.webhook.accepted",
        event_type=event.type,
        event_id=webhook_id,
        provider_message_id=event.data.email_id,
        duplicate=not was_new,
    )
    return WebhookAcceptedResponse(
        ok=True, duplicate=not was_new, received_event_id=webhook_id
    )


@router.get(
    "/v1/admin/email/events",
    response_model=EmailEventList,
)
async def list_email_events(
    operator: CurrentOperatorDep,  # noqa: ARG001
    db: SessionDep,
    limit: int = 100,
) -> EmailEventList:
    rows = await service.list_recent_events(db, limit=limit)
    return EmailEventList(
        items=[
            EmailEventView(
                id=r.id,
                provider_event_id=r.provider_event_id,
                email_send_id=r.email_send_id,
                provider_message_id=r.provider_message_id,
                event_type=r.event_type,
                to_address=r.to_address,
                received_at=r.received_at,
            )
            for r in rows
        ]
    )
