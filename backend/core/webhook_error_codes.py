"""Stable, non-sensitive webhook failure summaries for persisted errors."""
from __future__ import annotations


def classify_webhook_failure(err: BaseException | str) -> str:
    """Return a short stable code/summary safe to store in WebhookEvent.last_error."""
    if isinstance(err, str):
        code = err.strip()
        if not code:
            return "error"
        if len(code) > 120:
            return code[:120]
        return code

    name = type(err).__name__
    msg = str(err).strip()
    if not msg:
        return name

    for token in (
        "product_hydration_failed",
        "product_sync_incomplete",
        "customer_sync_incomplete",
        "customer_sync_row_failed",
        "order_status_missing_order",
    ):
        if token in msg:
            return f"{name}:{token}"

    return name
