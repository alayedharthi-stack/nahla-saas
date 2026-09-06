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
_PAYMENT_CONFIRMED_STATUSES = frozenset({
    "paid",
    "payment_completed",
    "payment_confirmed",
})
# Acceptance / merchant confirmed — not preparing. ``processing`` is preparing.
_CONFIRMATION_STATUSES = frozenset({
    "under_review",
    "pending",
    "in_review",
    "confirmed",
    "new",
})
_PREPARING_STATUSES = frozenset({
    "in_progress",
    "processing",
    "preparing",
})
_READY_STATUSES = frozenset({
    "ready",
    "ready_for_pickup",
    "packed",
})
_COD_METHODS = frozenset({
    "cod",
    "cash_on_delivery",
    "cod_payment",
    "cash",
})


def _payment_method(normalized_order: Mapping[str, Any]) -> str:
    raw = normalized_order.get("payment_method")
    if isinstance(raw, dict):
        raw = raw.get("slug") or raw.get("name") or raw.get("code") or ""
    return str(raw or "").strip().lower()


def _is_cod(normalized_order: Mapping[str, Any]) -> bool:
    return _payment_method(normalized_order) in _COD_METHODS


def customer_relevant_state(raw_status: Any) -> str:
    """Collapse provider synonyms into one customer-relevant state bucket."""
    curr = normalize_status_slug(raw_status)
    if curr in _SHIPPED_STATUSES:
        return "shipped"
    if curr in _OUT_FOR_DELIVERY_STATUSES:
        return "out_for_delivery"
    if curr in _DELIVERED_STATUSES:
        return "delivered"
    if curr in _CANCELLED_STATUSES:
        return "cancelled"
    if curr in _REFUNDED_STATUSES:
        return "refunded"
    if curr in _RETURNED_STATUSES:
        return "returned"
    if curr in _PAYMENT_PENDING_STATUSES:
        return "payment_pending"
    if curr in _PAYMENT_CONFIRMED_STATUSES:
        return "paid"
    if curr in _CONFIRMATION_STATUSES:
        return "confirmed"
    if curr in _PREPARING_STATUSES:
        return "preparing"
    if curr in _READY_STATUSES:
        return "ready"
    return curr


def normalize_salla_lifecycle_customer_state(raw_status: Any) -> str:
    return customer_relevant_state(raw_status)


def _is_poll_first_observation(normalized_order: Mapping[str, Any]) -> bool:
    observation = str(
        normalized_order.get("lifecycle_observation") or ""
    ).strip().lower()
    return observation in {"poll", "poll_import", "storesync_poll", "historical"}


def _first_seen_acceptance_intent(
    curr: str,
    normalized_order: Mapping[str, Any],
) -> Optional[BusinessIntent]:
    # Historical / poller first inserts are snapshots, not transitions.
    if _is_poll_first_observation(normalized_order):
        return None
    if curr in _PREPARING_STATUSES or curr in _READY_STATUSES:
        return None
    if curr in _CONFIRMATION_STATUSES:
        # Salla under_review is merchant acceptance, not a customer
        # confirm/cancel prompt. Nahla-origin COD confirmation is owned
        # exclusively by the checkout sender.
        return BusinessIntent.ORDER_CONFIRMED
    if curr in _PAYMENT_PENDING_STATUSES:
        if _is_cod(normalized_order):
            return None
        return BusinessIntent.PAYMENT_NEEDED
    return None


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
        return _first_seen_acceptance_intent(curr, normalized_order)

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
    if curr in _READY_STATUSES and prev not in _READY_STATUSES:
        return BusinessIntent.ORDER_PACKED
    if (
        curr in _PREPARING_STATUSES
        and prev not in _PREPARING_STATUSES
        and prev not in _READY_STATUSES
        and prev not in _SHIPPED_STATUSES
        and prev not in _OUT_FOR_DELIVERY_STATUSES
        and prev not in _DELIVERED_STATUSES
    ):
        return BusinessIntent.ORDER_PREPARING
    if (
        curr in _PAYMENT_CONFIRMED_STATUSES
        and prev in _PAYMENT_PENDING_STATUSES
        and not _is_cod(normalized_order)
    ):
        return BusinessIntent.PAYMENT_CONFIRMED
    if curr in _PAYMENT_PENDING_STATUSES and prev not in _PAYMENT_PENDING_STATUSES:
        if _is_cod(normalized_order):
            return None
        return BusinessIntent.PAYMENT_NEEDED
    if curr in _CONFIRMATION_STATUSES and prev not in _CONFIRMATION_STATUSES:
        if prev in _PAYMENT_PENDING_STATUSES and not _is_cod(normalized_order):
            return BusinessIntent.ORDER_CONFIRMED
        return None

    return None


__all__ = [
    "customer_relevant_state",
    "normalize_salla_lifecycle_business_intent",
    "normalize_salla_lifecycle_customer_state",
]
