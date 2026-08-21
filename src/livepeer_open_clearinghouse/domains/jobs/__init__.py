"""Jobs domain.

Handoff-mode equivalent of the sessions domain for one-shot unary,
multipart, and streaming workloads routed through ``paid-job/v1``.

Structurally identical to sessions: each job creates a
``payment_session`` row under the hood with the ``paid-job/v1`` protocol.
The split exists at the endpoint surface because customers (and
their SDKs) treat jobs and sessions as distinct concepts:

  - Jobs are one-shot. They mint, the SDK calls the broker once,
    settles with actual units, and closes. No refills.
  - Sessions are long-running. They mint, refill on broker-emitted
    balance-low, and close on customer/operator signal.

Both share the underlying ``payment_session`` table. Job accounting is
authorized by the broker's signed settlement and checked against the
pinned route and quote before the encumbrance is released.
"""
