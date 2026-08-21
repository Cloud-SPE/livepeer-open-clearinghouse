"""Mock broker server for SDK conformance testing.

The broker is the data-plane peer of LOC: the SDK opens a broker
session (`POST /v1/session`) and then either streams work or posts refills
to the broker-issued ``control.topup_url``. This mock implements the
HTTP subset needed to drive conformance scenarios for case-(d-*)
sessions.

The mock also serves `POST /v1/job` and the retained settlement lookup used by
`paid-job/v1` streaming clients. It intentionally implements protocol names and
declared axes only; the removed v0 interaction-mode taxonomy is not accepted.
"""
