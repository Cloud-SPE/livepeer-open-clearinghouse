"""Sessions domain.

Models long-running payment sessions opened by `POST /v1/sessions`
under exec-plan 002 (handoff mode). A `PaymentSession` row carries
the full session lifecycle — protocol, state, encumbered funded value,
final billed value and settlement outcome. Terminal accounting is
authorized by the broker-signed settlement chain.

A `PaymentSettlement` row records each event that affects a session's
accounting: authorization revisions, reconciliation observations, and the
final close. Funding tickets belong to the shared payer-payee wholesale
account, never to an individual session.
"""

from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
    SpendAuthorizationGrant,
)

__all__ = ["PaymentSession", "PaymentSettlement", "SpendAuthorizationGrant"]
