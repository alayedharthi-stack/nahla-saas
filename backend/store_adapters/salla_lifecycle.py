"""
Salla-owned external lifecycle status → BusinessIntent mapping (PR 2C).

Mapping lives in the adapter layer; core producers must not hardcode Salla slugs.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from core.commerce_lifecycle.intents import BusinessIntent
from store_integration.lifecycle_normalization import normalize_status_slug

_SHIPPED_STATUSES = frozenset({"shipped", "in_transit"})
_OUT_FOR_DELIVERY_STATUSES = frozenset({"out_for_delivery", "delivering"})
_DELIVERED_STATUSES = frozenset({"delivered"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "canceled_by_admin"})
_REFUNDED_STATUSES = frozenset({"refunded", "refund"})
_RETURNED_STATUSES = frozenset({"returned", "return"})
_PAYMENT_PENDING_STATUSES = frozenset({
    "payment_pending",
    "waiting_payment",
    "awaiting_payment",
    "pending_payment",
})
_CONFIRMATION_STATUSES = frozenset({
    "under_review",
    "pending",
    "in_review",
    "processing",
    "confirmed",
    "new",
})


def normalize_salla_lifecycle_business_intent(
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Mapping[str, Any],
) -> Optional[BusinessIntent]:
    prev = normalize_status_slug(raw_previous_status)
    curr = normalize_status_slug(raw_current_status)
    if not curr or (prev and prev == curr):
        return None

    has_prior = bool(prev) and prev != "unknown"

    if not has_prior:
        if curr in _CONFIRMATION_STATUSES:
            return BusinessIntent.ORDER_CONFIRMED
        if curr in _PAYMENT_PENDING_STATUSES:
            return BusinessIntent.PAYMENT_NEEDED
        return None

    if curr in _CANCELLED_STATUSES and prev not in _CANCELLED_STATUSES:
        return BusinessIntent.ORDER_CANCELLED
    if curr in _REFUNDED_STATUSES and prev not in _REFUNDED_STATUSES:
        return BusinessIntent.ORDER_REFUNDED
    if curr in _RETURNED_STATUSES and prev not in _RETURNED_STATUSES:
        return BusinessIntent.ORDER_RETURNED
    if curr in _DELIVERED_STATUSES and prev not in _DELIVERED_STATUSES:
        return BusinessIntent.ORDER_DELIVERED
    if curr in _OUT_FOR_DELIVERY_STATUSES and prev not in _OUT_FOR_DELIVERY_STATUSES:
        return BusinessIntent.OUT_FOR_DELIVERY
    if (
        curr in _SHIPPED_STATUSES
        and prev not in _SHIPPED_STATUSES
        and prev not in _OUT_FOR_DELIVERY_STATUSES
        and prev not in _DELIVERED_STATUSES
    ):
        return BusinessIntent.SHIPMENT_AVAILABLE
    if curr in _PAYMENT_PENDING_STATUSES and prev not in _PAYMENT_PENDING_STATUSES:
        return BusinessIntent.PAYMENT_NEEDED

    return None


__all__ = ["normalize_salla_lifecycle_business_intent"]
