"""Jobs domain.

Handoff-mode equivalent of the sessions domain for cases (a)/(b)/(c)
of exec-plan 002 — atomic, post-settled, and streaming workloads
that route over the upstream ``http-*@v0`` interaction modes.

Structurally identical to sessions: each job creates a
``payment_session`` row under the hood with a job-class mode
(``http-reqresp@v0`` / ``http-stream@v0`` / ``http-multipart@v0``).
The split exists at the endpoint surface because customers (and
their SDKs) treat jobs and sessions as distinct concepts:

  - Jobs are one-shot. They mint, the SDK calls the broker once,
    settles with actual units, and closes. No refills.
  - Sessions are long-running. They mint, refill on broker-emitted
    balance-low, and close on customer/operator signal.

Both share the underlying ``payment_session`` table, the
reconciliation janitor, and the trust model (daemon ledger
authoritative).
"""
