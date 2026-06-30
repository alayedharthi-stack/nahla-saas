"""
core/order_delivered_stamp.py
─────────────────────────────
Operational delivery timestamp for post-delivery automations.

When an order reaches ``status=delivered``, Nahla stamps
``Order.extra_metadata.delivered_at`` once (UTC ISO). The review-request
sweeper in ``core.post_delivery_review_request`` reads this field — no LLM,
no outbound send here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm.attributes import flag_modified


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _utc_iso(now: datetime) -> str:
    ts = now
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat()


def stamp_order_delivered_at_if_needed(
    order: Any,
    *,
    previous_status: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """
    Stamp ``extra_metadata.delivered_at`` when the order is delivered and the
    timestamp is not already set.

    Returns True when a new stamp was written. Never overwrites an existing
    ``delivered_at`` value.
    """
    new_status = _normalize_status(getattr(order, "status", ""))
    if new_status != "delivered":
        return False

    meta = dict(getattr(order, "extra_metadata", None) or {})
    if meta.get("delivered_at"):
        return False

    ts = now or datetime.now(timezone.utc)
    meta["delivered_at"] = _utc_iso(ts)
    order.extra_metadata = meta
    flag_modified(order, "extra_metadata")
    return True


def apply_order_status(
    order: Any,
    new_status: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Set ``order.status`` and stamp ``delivered_at`` when entering delivered."""
    previous = _normalize_status(getattr(order, "status", ""))
    order.status = new_status
    return stamp_order_delivered_at_if_needed(
        order,
        previous_status=previous,
        now=now,
    )


__all__ = [
    "apply_order_status",
    "stamp_order_delivered_at_if_needed",
]
