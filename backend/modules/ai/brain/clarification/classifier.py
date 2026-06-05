"""
clarification/classifier.py
───────────────────────────
Evidence-based missing-information classification.

Uses intent, state, and operational signals — not customer phrase matching.
"""
from __future__ import annotations

from typing import Any

from ..types import (
    INTENT_ASK_LOCATION,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_GENERAL,
    INTENT_HESITATION,
    INTENT_TRACK_ORDER,
    BrainContext,
)
from .evidence import build_clarification_evidence
from .types import (
    AMBIGUITY_MISSING_CUSTOMER_PREFERENCE,
    AMBIGUITY_MISSING_INTENT,
    AMBIGUITY_MISSING_LOCATION_DETAIL,
    AMBIGUITY_MISSING_OBJECTIVE,
    AMBIGUITY_MISSING_ORDER_REF,
    AMBIGUITY_MISSING_PAYMENT_TOPIC,
    AMBIGUITY_MISSING_PRODUCT_REF,
    AMBIGUITY_MISSING_SHIPPING_DETAIL,
    AMBIGUITY_MISSING_VARIANT,
    COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
    COMPOSE_TOPIC_SOLUTION_SEEKING,
    ClarificationSpec,
    RECOVERY_DETERMINISTIC,
    RECOVERY_GENERATIVE,
)


def _has_resolved_product_query(ctx: BrainContext) -> bool:
    try:
        from ..product_discovery_gate import _resolved_product_query  # noqa: PLC0415

        return bool(_resolved_product_query(ctx))
    except Exception:  # noqa: BLE001
        intent = getattr(ctx, "intent", None)
        slots = getattr(intent, "slots", None) or {}
        return bool(
            str(slots.get("product_query") or slots.get("product_name") or "").strip()
        )


def _is_unit_only_price_turn(ctx: BrainContext) -> bool:
    try:
        from ..product_discovery_gate import _is_unit_only_price_message  # noqa: PLC0415

        return _is_unit_only_price_message(ctx.message or "")
    except Exception:  # noqa: BLE001
        return False


def classify_missing_information(
    ctx: BrainContext,
    *,
    trigger: str = "",
) -> ClarificationSpec:
    """
    Classify what information is missing and how clarification should recover.

    Deterministic when the missing *field* is known (variant pick, list pick).
    Generative when the system must infer what to ask from conversation context.
    """
    evidence = build_clarification_evidence(ctx, trigger=trigger)
    intent = getattr(ctx, "intent", None)
    intent_name = str(getattr(intent, "name", "") or "")
    state = getattr(ctx, "state", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})

    candidate_titles = list(evidence.get("search_candidate_titles") or [])
    if candidate_titles:
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_CUSTOMER_PREFERENCE,
            recovery_mode=RECOVERY_DETERMINISTIC,
            evidence=evidence,
            trigger=trigger,
            structured_prompt={
                "field": "list_pick",
                "options": candidate_titles,
            },
        )

    if focus and _is_unit_only_price_turn(ctx):
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_VARIANT,
            recovery_mode=RECOVERY_DETERMINISTIC,
            evidence=evidence,
            trigger=trigger,
            structured_prompt={
                "field": "variant",
                "product_title": str(focus.get("title") or "").strip(),
            },
        )

    try:
        from ..commerce.solution_seeking import classify_solution_seeking_commerce  # noqa: PLC0415

        _ss = classify_solution_seeking_commerce(ctx.message or "")
        if _ss is not None:
            ev = dict(evidence)
            ev["solution_axis"] = _ss.axis
            return ClarificationSpec(
                ambiguity_class=AMBIGUITY_MISSING_OBJECTIVE,
                recovery_mode=RECOVERY_GENERATIVE,
                evidence=ev,
                trigger=trigger,
                compose_topic=COMPOSE_TOPIC_SOLUTION_SEEKING,
            )
    except Exception:  # noqa: BLE001
        pass

    if intent_name in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT):
        if not focus and not _has_resolved_product_query(ctx):
            return ClarificationSpec(
                ambiguity_class=AMBIGUITY_MISSING_PRODUCT_REF,
                recovery_mode=RECOVERY_GENERATIVE,
                evidence=evidence,
                trigger=trigger,
                compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
            )

    if intent_name == INTENT_ASK_PAYMENT_INFO:
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_PAYMENT_TOPIC,
            recovery_mode=RECOVERY_GENERATIVE,
            evidence=evidence,
            trigger=trigger,
            compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
        )

    if intent_name == INTENT_TRACK_ORDER:
        if not evidence.get("has_order_prep"):
            return ClarificationSpec(
                ambiguity_class=AMBIGUITY_MISSING_ORDER_REF,
                recovery_mode=RECOVERY_GENERATIVE,
                evidence=evidence,
                trigger=trigger,
                compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
            )

    if intent_name == INTENT_ASK_SHIPPING:
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_SHIPPING_DETAIL,
            recovery_mode=RECOVERY_GENERATIVE,
            evidence=evidence,
            trigger=trigger,
            compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
        )

    if intent_name == INTENT_ASK_LOCATION:
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_LOCATION_DETAIL,
            recovery_mode=RECOVERY_GENERATIVE,
            evidence=evidence,
            trigger=trigger,
            compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
        )

    if intent_name in (INTENT_GENERAL, INTENT_HESITATION):
        return ClarificationSpec(
            ambiguity_class=AMBIGUITY_MISSING_INTENT,
            recovery_mode=RECOVERY_GENERATIVE,
            evidence=evidence,
            trigger=trigger,
            compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
        )

    return ClarificationSpec(
        ambiguity_class=AMBIGUITY_MISSING_OBJECTIVE,
        recovery_mode=RECOVERY_GENERATIVE,
        evidence=evidence,
        trigger=trigger,
        compose_topic=COMPOSE_TOPIC_CONTEXTUAL_CLARIFY,
    )


def would_action_for_spec(spec: ClarificationSpec) -> str:
    """Predicted decision action for shadow telemetry."""
    if spec.is_deterministic and spec.structured_prompt:
        return "clarify"
    if spec.is_generative:
        return "llm_reply"
    return "clarify"


__all__ = [
    "classify_missing_information",
    "would_action_for_spec",
]
