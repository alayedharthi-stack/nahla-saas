"""
brain/clarification
────────────────────
Missing-information classification and contextual clarification routing.

Phase 0: shadow telemetry (``CLARIFICATION_SHADOW_ENABLED``, default on).
Phase A/1: generative contextual clarify (``CONTEXTUAL_CLARIFY_ENABLED`` — enable on
staging via env; default off until rollout gate passes).
"""
from .classifier import classify_missing_information, would_action_for_spec
from .flags import is_clarification_shadow_enabled, is_contextual_clarify_enabled
from .router import (
    record_clarification_shadow,
    try_contextual_clarification_fallback,
    try_contextual_price_clarification,
)
from .resolved_product_guard import (
    apply_resolved_product_clarify_guard,
    extract_resolved_product_subject,
    has_resolved_product_subject,
)
from .types import (
    AMBIGUITY_MISSING_PRODUCT_REF,
    COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
    ClarificationSpec,
    RECOVERY_DETERMINISTIC,
    RECOVERY_GENERATIVE,
)

__all__ = [
    "AMBIGUITY_MISSING_PRODUCT_REF",
    "COMPOSE_TOPIC_CONTEXTUAL_CLARIFY",
    "ClarificationSpec",
    "RECOVERY_DETERMINISTIC",
    "RECOVERY_GENERATIVE",
    "apply_resolved_product_clarify_guard",
    "classify_missing_information",
    "extract_resolved_product_subject",
    "has_resolved_product_subject",
    "is_clarification_shadow_enabled",
    "is_contextual_clarify_enabled",
    "record_clarification_shadow",
    "try_contextual_clarification_fallback",
    "try_contextual_price_clarification",
    "would_action_for_spec",
]
