"""
intent_priority/types.py
────────────────────────
Contracts for Customer Intent Priority Layer (AI-ARCH-007).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# ── Detected element categories (closed, extensible) ───────────────────────────
ELEMENT_COURTESY = "courtesy"
ELEMENT_GREETING = "greeting"
ELEMENT_BLESSING = "blessing"
ELEMENT_PRICE_INQUIRY = "price_inquiry"
ELEMENT_QUANTITY_UNIT = "quantity_unit"
ELEMENT_PRODUCT_AVAILABILITY = "product_availability"
ELEMENT_LOCATION_REQUEST = "location_request"
ELEMENT_SHIPPING_INQUIRY = "shipping_inquiry"
ELEMENT_STAFF_CONTACT = "staff_contact"
ELEMENT_PRODUCT_REFERENCE = "product_reference"
ELEMENT_ORDER_REQUEST = "order_request"
ELEMENT_PAYMENT_INQUIRY = "payment_inquiry"
ELEMENT_IMAGE_ATTACHMENT = "image_attachment"

# ── Primary customer goals ─────────────────────────────────────────────────────
GOAL_PRICE_INQUIRY = "price_inquiry"
GOAL_PRODUCT_AVAILABILITY = "product_availability"
GOAL_LOCATION_REQUEST = "location_request"
GOAL_SHIPPING_INQUIRY = "shipping_inquiry"
GOAL_STAFF_CONTACT = "staff_contact"
GOAL_ORDER_REQUEST = "order_request"
GOAL_PAYMENT_INQUIRY = "payment_inquiry"
GOAL_SOCIAL_ONLY = "social_only"
GOAL_GREETING_ONLY = "greeting_only"
GOAL_GENERAL = "general"
GOAL_PRODUCT_ORIGIN_VERIFICATION = "product_origin_verification"

# Commercial elements outrank social openers.
_COMMERCIAL_ELEMENT_TYPES = frozenset({
    ELEMENT_PRICE_INQUIRY,
    ELEMENT_QUANTITY_UNIT,
    ELEMENT_PRODUCT_AVAILABILITY,
    ELEMENT_LOCATION_REQUEST,
    ELEMENT_SHIPPING_INQUIRY,
    ELEMENT_STAFF_CONTACT,
    ELEMENT_PRODUCT_REFERENCE,
    ELEMENT_ORDER_REQUEST,
    ELEMENT_PAYMENT_INQUIRY,
})

_SOCIAL_ELEMENT_TYPES = frozenset({
    ELEMENT_COURTESY,
    ELEMENT_GREETING,
    ELEMENT_BLESSING,
})

_ELEMENT_PRIORITY_WEIGHT: Dict[str, int] = {
    ELEMENT_PRICE_INQUIRY: 100,
    ELEMENT_QUANTITY_UNIT: 95,
    ELEMENT_PRODUCT_AVAILABILITY: 98,
    ELEMENT_LOCATION_REQUEST: 97,
    ELEMENT_SHIPPING_INQUIRY: 96,
    ELEMENT_STAFF_CONTACT: 94,
    ELEMENT_ORDER_REQUEST: 93,
    ELEMENT_PAYMENT_INQUIRY: 92,
    ELEMENT_PRODUCT_REFERENCE: 90,
    ELEMENT_IMAGE_ATTACHMENT: 88,
    ELEMENT_COURTESY: 20,
    ELEMENT_BLESSING: 22,
    ELEMENT_GREETING: 18,
}

_GOAL_FROM_ELEMENT: Dict[str, str] = {
    ELEMENT_PRICE_INQUIRY: GOAL_PRICE_INQUIRY,
    ELEMENT_QUANTITY_UNIT: GOAL_PRICE_INQUIRY,
    ELEMENT_PRODUCT_AVAILABILITY: GOAL_PRODUCT_AVAILABILITY,
    ELEMENT_LOCATION_REQUEST: GOAL_LOCATION_REQUEST,
    ELEMENT_SHIPPING_INQUIRY: GOAL_SHIPPING_INQUIRY,
    ELEMENT_STAFF_CONTACT: GOAL_STAFF_CONTACT,
    ELEMENT_ORDER_REQUEST: GOAL_ORDER_REQUEST,
    ELEMENT_PAYMENT_INQUIRY: GOAL_PAYMENT_INQUIRY,
    ELEMENT_PRODUCT_REFERENCE: GOAL_PRODUCT_AVAILABILITY,
    ELEMENT_COURTESY: GOAL_SOCIAL_ONLY,
    ELEMENT_BLESSING: GOAL_SOCIAL_ONLY,
    ELEMENT_GREETING: GOAL_GREETING_ONLY,
}


@dataclass(frozen=True)
class DetectedElement:
    """A single conversational element extracted from the inbound turn."""
    element_type: str
    confidence: float
    span_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.element_type,
            "confidence": round(self.confidence, 2),
            "span_hint": self.span_hint,
        }


@dataclass
class IntentPriorityVerdict:
    """
    Customer Intent Priority verdict for one turn.

    Consumed by clarification, product discovery, and compose layers.
    """
    detected_elements: List[DetectedElement] = field(default_factory=list)
    primary_customer_goal: str = GOAL_GENERAL
    secondary_elements: List[str] = field(default_factory=list)
    priority_ranking: List[str] = field(default_factory=list)
    requires_clarification: bool = False
    clarification_reason: str = ""
    recommended_focus: str = ""

    def to_trace_dict(self) -> Dict[str, Any]:
        return {
            "detected_elements": [e.to_dict() for e in self.detected_elements],
            "primary_customer_goal": self.primary_customer_goal,
            "secondary_elements": list(self.secondary_elements),
            "priority_ranking": list(self.priority_ranking),
            "recommended_focus": self.recommended_focus,
            "requires_clarification": self.requires_clarification,
            "clarification_reason": self.clarification_reason or None,
        }

    @property
    def has_commercial_primary(self) -> bool:
        return self.primary_customer_goal not in {
            GOAL_SOCIAL_ONLY,
            GOAL_GREETING_ONLY,
            GOAL_GENERAL,
        }

    @property
    def has_secondary_social(self) -> bool:
        return bool(
            set(self.secondary_elements) & _SOCIAL_ELEMENT_TYPES
        )


__all__ = [
    "DetectedElement",
    "IntentPriorityVerdict",
    "ELEMENT_COURTESY",
    "ELEMENT_GREETING",
    "ELEMENT_BLESSING",
    "ELEMENT_PRICE_INQUIRY",
    "ELEMENT_QUANTITY_UNIT",
    "ELEMENT_PRODUCT_AVAILABILITY",
    "ELEMENT_LOCATION_REQUEST",
    "ELEMENT_SHIPPING_INQUIRY",
    "ELEMENT_STAFF_CONTACT",
    "ELEMENT_PRODUCT_REFERENCE",
    "ELEMENT_ORDER_REQUEST",
    "ELEMENT_PAYMENT_INQUIRY",
    "ELEMENT_IMAGE_ATTACHMENT",
    "GOAL_PRICE_INQUIRY",
    "GOAL_PRODUCT_AVAILABILITY",
    "GOAL_LOCATION_REQUEST",
    "GOAL_SHIPPING_INQUIRY",
    "GOAL_STAFF_CONTACT",
    "GOAL_ORDER_REQUEST",
    "GOAL_PAYMENT_INQUIRY",
    "GOAL_SOCIAL_ONLY",
    "GOAL_GREETING_ONLY",
    "GOAL_GENERAL",
    "GOAL_PRODUCT_ORIGIN_VERIFICATION",
    "_COMMERCIAL_ELEMENT_TYPES",
    "_SOCIAL_ELEMENT_TYPES",
    "_ELEMENT_PRIORITY_WEIGHT",
    "_GOAL_FROM_ELEMENT",
]
