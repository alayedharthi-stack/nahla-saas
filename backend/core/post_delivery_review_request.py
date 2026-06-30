"""
core/post_delivery_review_request.py
────────────────────────────────────
Post-delivery review request eligibility and idempotency markers.

Operational only: the system decides *when* a delivered order may receive
a one-time review request. Wording is resolved by the automation engine
via the merchant's ``review_request`` template binding — not by LLM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

POST_DELIVERY_REVIEW_DELAY_HOURS = 24

_TERMINAL_STATUSES = frozenset({
    "cancelled",
    "canceled",
    "refunded",
})


def review_request_already_sent(order: Any) -> bool:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    return meta.get("review_request_sent") is True


def read_delivered_at(order: Any) -> Optional[datetime]:
    """Read delivery timestamp from ``Order.extra_metadata.delivered_at``."""
    meta = dict(getattr(order, "extra_metadata", None) or {})
    cand = meta.get("delivered_at")
    if isinstance(cand, datetime):
        return cand.replace(tzinfo=None) if cand.tzinfo else cand
    if not cand:
        return None
    text = str(cand).strip()
    for variant in (
        text.replace("Z", "+00:00"),
        text.replace(" ", "T", 1),
        text.split(".", 1)[0].replace(" ", "T", 1),
    ):
        try:
            parsed = datetime.fromisoformat(variant)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except Exception:
            continue
    return None


def stamp_review_request_sent(
    order: Any,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return updated ``extra_metadata`` with one-time review send markers."""
    meta = dict(getattr(order, "extra_metadata", None) or {})
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    meta["review_request_sent"] = True
    meta["review_requested_at"] = ts.isoformat()
    return meta


def is_order_eligible_for_review_request(
    order: Any,
    *,
    now: datetime,
    delay_hours: int = POST_DELIVERY_REVIEW_DELAY_HOURS,
) -> bool:
    status = str(getattr(order, "status", "") or "").strip().lower()
    if status != "delivered":
        return False
    if status in _TERMINAL_STATUSES:
        return False
    if review_request_already_sent(order):
        return False

    delivered_at = read_delivered_at(order)
    if delivered_at is None:
        return False

    anchor = delivered_at
    if anchor.tzinfo is not None:
        anchor = anchor.astimezone(timezone.utc).replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    if (now_naive - anchor) < timedelta(hours=max(1, int(delay_hours or POST_DELIVERY_REVIEW_DELAY_HOURS))):
        return False
    return True


__all__ = [
    "POST_DELIVERY_REVIEW_DELAY_HOURS",
    "is_order_eligible_for_review_request",
    "read_delivered_at",
    "review_request_already_sent",
    "stamp_review_request_sent",
]
