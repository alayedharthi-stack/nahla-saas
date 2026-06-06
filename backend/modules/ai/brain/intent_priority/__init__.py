"""
brain/intent_priority
─────────────────────
Customer Intent Priority Layer (AI-ARCH-007).

Extracts and ranks conversational elements so commercial intent beats
courtesy/greeting openers. Consumed by clarification, product discovery,
and compose — platform-wide, tenant-agnostic.
"""
from .analyzer import (
    compute_customer_intent_priority,
    enrich_intent_with_priority,
)
from .compose_hints import (
    contextual_clarify_priority_hint,
    intent_priority_compose_directive,
)
from .telemetry import log_intent_priority_verdict
from .types import (
    DetectedElement,
    IntentPriorityVerdict,
    GOAL_LOCATION_REQUEST,
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SHIPPING_INQUIRY,
    GOAL_STAFF_CONTACT,
)

__all__ = [
    "DetectedElement",
    "IntentPriorityVerdict",
    "GOAL_PRICE_INQUIRY",
    "GOAL_PRODUCT_AVAILABILITY",
    "GOAL_LOCATION_REQUEST",
    "GOAL_SHIPPING_INQUIRY",
    "GOAL_STAFF_CONTACT",
    "compute_customer_intent_priority",
    "enrich_intent_with_priority",
    "intent_priority_compose_directive",
    "contextual_clarify_priority_hint",
    "log_intent_priority_verdict",
]
