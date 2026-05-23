"""Notifications domain.

Persists every outbound transactional email (`EmailSend`) and every
delivery event the upstream provider reports via webhook (`EmailEvent`).

The webhook endpoint at ``POST /v1/webhooks/resend`` is the only inbound
surface here; it's authenticated via the Standard Webhooks HMAC
signature (`webhook-id` + `webhook-timestamp` + `webhook-signature`
headers), not the API-key / cookie session that authenticates the rest
of the gateway.
"""
