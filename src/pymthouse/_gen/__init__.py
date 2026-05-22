"""Generated gRPC stubs (do not edit by hand).

The `protoc` plugin emits absolute imports like
``from livepeer.payments.v1 import payer_daemon_pb2`` because that's what
the proto file's package name resolves to. Rather than monkey-patch the
generated files, we insert this directory onto ``sys.path`` so those
top-level imports resolve from inside this package only.

Regenerate with ``make protoc``.
"""

from __future__ import annotations

import os
import sys

_GEN_DIR = os.path.dirname(__file__)
if _GEN_DIR not in sys.path:
    sys.path.insert(0, _GEN_DIR)
