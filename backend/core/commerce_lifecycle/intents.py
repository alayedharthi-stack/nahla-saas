"""
Canonical BusinessIntent values — platform-wide, provider-agnostic.
"""
from __future__ import annotations

from enum import Enum


class BusinessIntent(str, Enum):
    """What the merchant/customer needs to know — not a provider status slug."""

    INCOMPLETE_ORDER = "incomplete_order"
    ORDER_CONFIRMED = "order_confirmed"
    COD_CONFIRMATION = "cod_confirmation"
    PAYMENT_NEEDED = "payment_needed"
    PAYMENT_SUBMITTED = "payment_submitted"
    PAYMENT_CONFIRMED = "payment_confirmed"
    ORDER_PREPARING = "order_preparing"
    ORDER_PACKED = "order_packed"
    SHIPMENT_AVAILABLE = "shipment_available"
    OUT_FOR_DELIVERY = "out_for_delivery"
    ORDER_DELIVERED = "order_delivered"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REFUNDED = "order_refunded"
    ORDER_RETURNED = "order_returned"
    CUSTOMER_ACTION_REQUIRED = "customer_action_required"
    ADDRESS_REQUIRED = "address_required"
    REVIEW_REQUEST = "review_request"
    SUPPORT_FOLLOWUP = "support_followup"


# Deprecated internal alias — do not use in new code outside this package.
LifecycleIntent = BusinessIntent

__all__ = ["BusinessIntent", "LifecycleIntent"]
