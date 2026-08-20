"""Sessions domain.

Models long-running payment sessions opened by `POST /v1/sessions`
under exec-plan 002 (handoff mode). A `PaymentSession` row carries
the full session lifecycle — protocol, state, encumbered funded value,
final billed value, settlement outcome — and is the unit the
reconciliation janitor operates on.

A `PaymentSettlement` row records each event that affects a
session's accounting: refills granted/denied, balance-low
notifications, the final close. Multiple `payment` (ticket-mint)
rows may attach to the same session via `payment.session_id`.
"""

from livepeer_open_clearinghouse.domains.sessions.repo import (
    PaymentSession,
    PaymentSettlement,
)

__all__ = ["PaymentSession", "PaymentSettlement"]
