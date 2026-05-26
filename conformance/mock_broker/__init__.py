"""Mock broker server for SDK conformance testing.

The broker is the data-plane peer of LOC: the SDK opens a broker
session (POST /v1/cap) and then either streams work or posts refills
to the broker-issued ``control.topup_url``. This mock implements the
HTTP subset needed to drive conformance scenarios for case-(d-*)
sessions.

WebSocket-based modes (``session-control-plus-media@v0``,
``ws-realtime@v0``) are stubbed with the open + a single
acknowledgement frame; full streaming conformance lands when the
per-SDK runners need it.
"""
