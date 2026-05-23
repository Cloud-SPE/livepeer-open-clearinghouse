"""notifications domain config.

The 5-minute timestamp window comes from the Standard Webhooks spec —
events older than this are rejected as potential replays. Resend's
client retry budget fits comfortably inside that window.
"""

from datetime import timedelta

WEBHOOK_TIMESTAMP_TOLERANCE = timedelta(minutes=5)
