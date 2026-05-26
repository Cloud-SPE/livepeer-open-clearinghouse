"""ASGI middleware to decompress ``Content-Encoding: gzip`` request bodies.

FastAPI / Starlette transparently compress responses (via the optional
``GZipMiddleware``) but do NOT decompress inbound bodies. The v1 SDK
contract gzips telemetry batches > 1 KiB before posting, so we need
this middleware to keep the existing handlers ignorant of the wire
encoding.

Implementation is minimal — only handles HTTP scope, only acts when
the header is exactly ``gzip``. ``identity`` and missing headers
pass through unchanged. Other encodings (deflate, br) are out of
scope for v1 and treated as unsupported (the body would just hit
JSON-parsing and fail with 400, same as before this middleware).
"""

from __future__ import annotations

import gzip

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class GzipRequestMiddleware:
    """Drop-in ASGI middleware that decompresses gzipped HTTP bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        is_gzipped = any(
            k.decode("latin-1").lower() == "content-encoding"
            and v.decode("latin-1").strip().lower() == "gzip"
            for k, v in headers
        )
        if not is_gzipped:
            await self.app(scope, receive, send)
            return

        # Buffer the entire request body, then decompress and re-emit
        # via a wrapped receive callable. Telemetry batches are small
        # (capped at MAX_BATCH_SIZE * MAX_PAYLOAD_BYTES = 1000 * 16K =
        # ~16 MiB in the worst case) so buffering is acceptable.
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        try:
            decompressed = gzip.decompress(body)
        except OSError:
            # Malformed gzip body — let the downstream handler 400 on
            # the still-compressed bytes. Don't swallow with a custom
            # error; the existing FastAPI handler is fine.
            decompressed = body

        # Strip content-encoding + rewrite content-length so the
        # downstream handler doesn't trip on a length mismatch.
        new_headers: list[tuple[bytes, bytes]] = []
        for k, v in headers:
            name = k.decode("latin-1").lower()
            if name in ("content-encoding", "content-length"):
                continue
            new_headers.append((k, v))
        new_headers.append((b"content-length", str(len(decompressed)).encode("ascii")))
        scope = dict(scope)
        scope["headers"] = new_headers

        sent = False

        async def _receive() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {
                "type": "http.request",
                "body": decompressed,
                "more_body": False,
            }

        await self.app(scope, _receive, send)
