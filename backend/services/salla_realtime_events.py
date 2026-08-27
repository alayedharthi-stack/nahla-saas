"""
services/salla_realtime_events.py
Canonical Salla webhook event groupings for near-real-time commerce sync.

Official references:
- Merchant webhook list: https://docs.salla.dev/doc-421119
- Abandoned cart models: https://docs.salla.dev/doc-433812
- Abandoned cart API schema: https://docs.salla.dev/api-5394139
"""
from __future__ import annotations

from typing import FrozenSet, Literal

SALLA_ORDER_WEBHOOK_EVENTS: FrozenSet[str] = frozenset({
    "order.created",
    "order.updated",
    "order.status.updated",
    "order.payment.updated",
    "order.products.updated",
    "order.coupon.updated",
    "order.total.price.updated",
    "order.cancelled",
    "order.refunded",
    "order.deleted",
    "order.customer.updated",
})

SALLA_CUSTOMER_WEBHOOK_EVENTS: FrozenSet[str] = frozenset({
    "customer.created",
    "customer.updated",
})

SALLA_CUSTOMER_LOGIN_EVENTS: FrozenSet[str] = frozenset({
    "customer.login",
})

SALLA_PRODUCT_UPSERT_WEBHOOK_EVENTS: FrozenSet[str] = frozenset({
    "product.created",
    "product.price.updated",
    "product.status.updated",
    "product.image.updated",
    "product.category.updated",
    "product.brand.updated",
    "product.tags.updated",
    "product.quantity.low",
})

SALLA_MERCHANT_WEBHOOK_DEPRECATED_EVENTS: FrozenSet[str] = frozenset({
    "product.updated",
    "product.available",
})

SALLA_PRODUCT_DELETE_WEBHOOK_EVENTS: FrozenSet[str] = frozenset({
    "product.deleted",
})

SALLA_ABANDONED_CART_CREATE_EVENTS: FrozenSet[str] = frozenset({
    "abandoned.cart",
})

SALLA_ABANDONED_CART_UPDATE_EVENTS: FrozenSet[str] = frozenset({
    "abandoned.cart.updated",
})

SALLA_ABANDONED_CART_STATUS_EVENTS: FrozenSet[str] = frozenset({
    "abandoned.cart.status.changed",
})

SALLA_ABANDONED_CART_PURCHASED_EVENTS: FrozenSet[str] = frozenset({
    "abandoned.cart.purchased",
})

SALLA_WEBHOOK_COMPATIBILITY_ALIASES: FrozenSet[str] = frozenset({
    "cart.abandoned",
    "abandoned_cart",
})

SALLA_APP_FUNCTIONS_ONLY_EVENTS: FrozenSet[str] = frozenset({
    "abandoned.cart.updated",
})

SALLA_SPECIAL_OFFER_WEBHOOK_EVENTS: FrozenSet[str] = frozenset({
    "specialoffer.created",
    "specialoffer.updated",
})

SALLA_ABANDONED_CART_TERMINAL_STATUSES: FrozenSet[str] = frozenset({
    "purchased",
})

AbandonedCartEventKind = Literal["created", "updated", "status_changed", "purchased"]


def classify_abandoned_cart_event(event_type: str) -> AbandonedCartEventKind | None:
    if event_type in SALLA_ABANDONED_CART_CREATE_EVENTS | SALLA_WEBHOOK_COMPATIBILITY_ALIASES:
        return "created"
    if event_type in SALLA_ABANDONED_CART_UPDATE_EVENTS:
        return "updated"
    if event_type in SALLA_ABANDONED_CART_STATUS_EVENTS:
        return "status_changed"
    if event_type in SALLA_ABANDONED_CART_PURCHASED_EVENTS:
        return "purchased"
    return None


def is_terminal_abandoned_cart_status(status: str | None) -> bool:
    return str(status or "").strip().lower() in SALLA_ABANDONED_CART_TERMINAL_STATUSES


def is_salla_commerce_event(event_type: str) -> bool:
    return event_type in (
        SALLA_ORDER_WEBHOOK_EVENTS
        | SALLA_CUSTOMER_WEBHOOK_EVENTS
        | SALLA_CUSTOMER_LOGIN_EVENTS
        | SALLA_PRODUCT_UPSERT_WEBHOOK_EVENTS
        | SALLA_MERCHANT_WEBHOOK_DEPRECATED_EVENTS
        | SALLA_PRODUCT_DELETE_WEBHOOK_EVENTS
        | SALLA_ABANDONED_CART_CREATE_EVENTS
        | SALLA_ABANDONED_CART_UPDATE_EVENTS
        | SALLA_ABANDONED_CART_STATUS_EVENTS
        | SALLA_ABANDONED_CART_PURCHASED_EVENTS
        | SALLA_WEBHOOK_COMPATIBILITY_ALIASES
        | SALLA_SPECIAL_OFFER_WEBHOOK_EVENTS
    )


SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST: tuple[str, ...] = tuple(sorted(
    SALLA_ORDER_WEBHOOK_EVENTS
    | SALLA_CUSTOMER_WEBHOOK_EVENTS
    | SALLA_CUSTOMER_LOGIN_EVENTS
    | SALLA_PRODUCT_UPSERT_WEBHOOK_EVENTS
    | SALLA_PRODUCT_DELETE_WEBHOOK_EVENTS
    | SALLA_ABANDONED_CART_CREATE_EVENTS
    | SALLA_ABANDONED_CART_STATUS_EVENTS
    | SALLA_ABANDONED_CART_PURCHASED_EVENTS
    | SALLA_SPECIAL_OFFER_WEBHOOK_EVENTS
))

# Backward-compatible alias used by diagnostics.
SALLA_COMMERCE_WEBHOOK_REQUIRED_EVENTS = SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST


def event_registry_contract() -> dict[str, object]:
    return {
        "merchant_active": sorted(
            SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST
            + tuple(sorted(SALLA_MERCHANT_WEBHOOK_DEPRECATED_EVENTS))
            + tuple(sorted(SALLA_ABANDONED_CART_UPDATE_EVENTS))
        ),
        "merchant_deprecated": sorted(SALLA_MERCHANT_WEBHOOK_DEPRECATED_EVENTS),
        "app_functions_only": sorted(SALLA_APP_FUNCTIONS_ONLY_EVENTS),
        "compatibility_aliases": sorted(SALLA_WEBHOOK_COMPATIBILITY_ALIASES),
        "activation_checklist": list(SALLA_MERCHANT_WEBHOOK_ACTIVATION_CHECKLIST),
    }
