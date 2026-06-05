"""
clarification/types.py
──────────────────────
Platform-wide missing-information model for clarification routing.

Closed enums only — no merchant-specific or message-phrase special cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ── Ambiguity classes (what is missing) ───────────────────────────────────────

AMBIGUITY_MISSING_PRODUCT_REF = "missing_product_ref"
AMBIGUITY_MISSING_VARIANT = "missing_variant"
AMBIGUITY_MISSING_QUANTITY = "missing_quantity"
AMBIGUITY_MISSING_PAYMENT_TOPIC = "missing_payment_topic"
AMBIGUITY_MISSING_ORDER_REF = "missing_order_ref"
AMBIGUITY_MISSING_SHIPPING_DETAIL = "missing_shipping_detail"
AMBIGUITY_MISSING_LOCATION_DETAIL = "missing_location_detail"
AMBIGUITY_MISSING_CUSTOMER_PREFERENCE = "missing_customer_preference"
AMBIGUITY_MISSING_OBJECTIVE = "missing_objective"
AMBIGUITY_MISSING_INTENT = "missing_intent"

# ── Recovery modes ──────────────────────────────────────────────────────────

RECOVERY_DETERMINISTIC = "deterministic"
RECOVERY_GENERATIVE = "generative"

# ── Compose topics (generative path) ────────────────────────────────────────

COMPOSE_TOPIC_CONTEXTUAL_CLARIFY = "contextual_clarify"
COMPOSE_TOPIC_SOLUTION_SEEKING = "solution_seeking_commerce"


@dataclass(frozen=True)
class ClarificationSpec:
    """Typed clarification contract produced by the classifier."""

    ambiguity_class: str
    recovery_mode: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    compose_topic: str = COMPOSE_TOPIC_CONTEXTUAL_CLARIFY
    structured_prompt: Optional[Dict[str, Any]] = None
    trigger: str = ""

    @property
    def is_generative(self) -> bool:
        return self.recovery_mode == RECOVERY_GENERATIVE

    @property
    def is_deterministic(self) -> bool:
        return self.recovery_mode == RECOVERY_DETERMINISTIC


__all__ = [
    "AMBIGUITY_MISSING_CUSTOMER_PREFERENCE",
    "AMBIGUITY_MISSING_INTENT",
    "AMBIGUITY_MISSING_LOCATION_DETAIL",
    "AMBIGUITY_MISSING_OBJECTIVE",
    "AMBIGUITY_MISSING_ORDER_REF",
    "AMBIGUITY_MISSING_PAYMENT_TOPIC",
    "AMBIGUITY_MISSING_PRODUCT_REF",
    "AMBIGUITY_MISSING_QUANTITY",
    "AMBIGUITY_MISSING_SHIPPING_DETAIL",
    "AMBIGUITY_MISSING_VARIANT",
    "COMPOSE_TOPIC_CONTEXTUAL_CLARIFY",
    "COMPOSE_TOPIC_SOLUTION_SEEKING",
    "ClarificationSpec",
    "RECOVERY_DETERMINISTIC",
    "RECOVERY_GENERATIVE",
]
