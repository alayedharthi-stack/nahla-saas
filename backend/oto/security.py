"""Webhook validation based on OTO's documented signed event fields."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Mapping


def verify_webhook(
    payload: Mapping[str, Any], *,
    secret: str,
    event_type: str,
    now_ms: int | None = None,
    max_age_ms: int = 5 * 60 * 1000,
) -> bool:
    """Reject stale, unsigned, malformed, or incorrectly signed events."""
    if not secret or event_type not in {"orderStatus", "shipmentError"}:
        return False
    order_id = payload.get("orderId")
    value = payload.get("status" if event_type == "orderStatus" else "errorCode")
    signature = payload.get("signature")
    timestamp = payload.get("timestamp")
    if not all(isinstance(x, str) and x for x in (order_id, value, signature)):
        return False
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now - stamp) > max_age_ms:
        return False
    message = f"{order_id}:{value}:{stamp}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(signature, expected)


def webhook_fingerprint(payload: Mapping[str, Any], event_type: str) -> str:
    """Stable duplicate key, without storing signatures or customer data."""
    fields = (
        event_type,
        str(payload.get("orderId") or ""),
        str(payload.get("status") or payload.get("errorCode") or ""),
        str(payload.get("timestamp") or ""),
        str(payload.get("signature") or ""),
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()
