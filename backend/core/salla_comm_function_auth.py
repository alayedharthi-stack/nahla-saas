"""Independent, path-bound authentication for Salla Communication App Functions.

This key is deliberately distinct from Salla's public webhook signing key.
Never log the key, signature, or message body. The caller must durably reserve
the nonce before processing the request.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Mapping

FUNCTION_PATH = "/webhook/salla/communication-function"
MAX_SKEW_SECONDS = 300
_NONCE = re.compile(r"^[a-f0-9]{32}$")
_SIGNATURE = re.compile(r"^[a-f0-9]{64}$")


def signature_message(body: bytes, timestamp: str, nonce: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"v1\nPOST\n{FUNCTION_PATH}\n{timestamp}\n{nonce}\n{digest}".encode("ascii")


def verify_function_request(
    body: bytes, headers: Mapping[str, str], *, secret: str | None = None,
    now: int | None = None,
) -> tuple[bool, str | None]:
    """Reject missing keys, bad time/nonce, altered bodies and wrong signatures."""
    key = secret if secret is not None else os.getenv("SALLA_COMM_FUNCTION_HMAC_SECRET", "")
    if not isinstance(key, str) or len(key) < 32:
        return False, None
    # A misconfigured shared key would let a public webhook signature be
    # promoted into this privileged send channel. Reject that configuration.
    from core.config import SALLA_WEBHOOK_SECRET
    if SALLA_WEBHOOK_SECRET and hmac.compare_digest(key, SALLA_WEBHOOK_SECRET):
        return False, None
    timestamp = str(headers.get("x-nahla-comm-timestamp") or "")
    nonce = str(headers.get("x-nahla-comm-nonce") or "")
    signature = str(headers.get("x-nahla-comm-signature") or "")
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        return False, None
    if not _NONCE.fullmatch(nonce) or not _SIGNATURE.fullmatch(signature):
        return False, None
    current = int(time.time()) if now is None else int(now)
    if abs(current - int(timestamp)) > MAX_SKEW_SECONDS:
        return False, None
    expected = hmac.new(
        key.encode("utf-8"), signature_message(body, timestamp, nonce), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, None
    return True, nonce
