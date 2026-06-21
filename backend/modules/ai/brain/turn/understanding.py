"""
turn/understanding.py
─────────────────────
Phase 1 — synthesize TurnUnderstanding from existing pipeline signals.

Read-only: consumes intent, intent_priority, state_relevance, social_human_context,
and persisted state. Does NOT add new regex patterns.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ..types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_COMPLAINT_REFUND,
    INTENT_GREETING,
    INTENT_PAY_NOW,
    INTENT_SOCIAL,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_WHO_ARE_YOU,
    BrainContext,
    MerchantConversationState,
)
from .contract import StateConflict, TurnUnderstanding, UnderstandingEvidence

_CHECKOUT_STAGES = frozenset({"ordering", "deciding", "checkout"})


def _evidence(
    kind: str,
    source: str,
    ref: str,
    summary: str,
    weight: float = 0.5,
) -> UnderstandingEvidence:
    return UnderstandingEvidence(
        kind=kind,
        source=source,
        ref=ref,
        summary=summary,
        weight=weight,
    )


def _has_active_checkout_workflow(state: MerchantConversationState) -> bool:
    op = state.order_prep
    if str(getattr(op, "product_id", "") or "").strip():
        return True
    if list(getattr(op, "missing_fields", None) or []):
        return True
    if getattr(op, "awaiting_payment_receipt", False):
        return True
    if getattr(op, "awaiting_variant_choice", False):
        return True
    if str(state.stage or "") in _CHECKOUT_STAGES and state.current_product_focus:
        return True
    if state.last_question_asked and not state.last_question_answered:
        return True
    return False


def _derive_active_objective_candidate(
    state: MerchantConversationState,
    state_relevance: Any,
) -> Optional[str]:
    workflows = list(getattr(state_relevance, "active_workflows", None) or [])
    op = state.order_prep
    missing = list(getattr(op, "missing_fields", None) or [])

    if "awaiting_payment_receipt" in workflows or getattr(op, "awaiting_payment_receipt", False):
        return "await_payment_receipt"
    if missing:
        if set(missing) & {"city", "google_maps_url", "short_address_code", "address_line"}:
            return "collect_fulfillment_city"
        return f"collect_checkout_slots:{','.join(missing[:3])}"
    if getattr(op, "awaiting_variant_choice", False):
        return "await_variant_choice"
    if str(state.stage or "") in _CHECKOUT_STAGES:
        return f"resume_{state.stage}_flow"
    if state.last_question_asked and not state.last_question_answered:
        return f"replay_pending_question:{state.last_question_asked[:40]}"
    if workflows:
        return f"resume_workflow:{workflows[0]}"
    return None


def _derive_semantics(
    *,
    intent_name: str,
    intent_confidence: float,
    primary_goal: str,
    shc: Any,
    intent_priority: Any,
) -> Tuple[str, str, str, List[UnderstandingEvidence]]:
    evidence: List[UnderstandingEvidence] = []
    evidence.append(_evidence(
        "intent_classification",
        "intent_classifier",
        f"intent.{intent_name}",
        f"classified intent={intent_name} confidence={intent_confidence:.2f}",
        weight=min(0.95, float(intent_confidence or 0.5)),
    ))

    if primary_goal:
        evidence.append(_evidence(
            "customer_goal",
            "intent_priority",
            f"goal.{primary_goal}",
            f"primary_customer_goal={primary_goal}",
            weight=0.85,
        ))

    shc_category = str(getattr(shc, "social_category", "") or "")
    is_pure_social = bool(getattr(shc, "is_pure_social_turn", False))
    if shc_category:
        evidence.append(_evidence(
            "social_context",
            "social_human_context",
            f"shc.{shc_category}",
            f"social_category={shc_category} pure_social={is_pure_social}",
            weight=0.8 if is_pure_social else 0.6,
        ))

    # Map existing intent signals → understanding fields (no new regex).
    if intent_name == INTENT_COMPLAINT_REFUND:
        return (
            "complaint_refund",
            "product_quality_dispute",
            "resolve_complaint_or_refund",
            evidence,
        )

    if intent_name in {INTENT_SOCIAL, INTENT_GREETING, INTENT_WHO_ARE_YOU}:
        topic = shc_category or ("identity" if intent_name == INTENT_WHO_ARE_YOU else "social_interaction")
        goal = (
            "answer_identity"
            if intent_name == INTENT_WHO_ARE_YOU
            else ("acknowledge_social" if is_pure_social else "respond_socially")
        )
        return ("social_interaction", topic, goal, evidence)

    if intent_name == INTENT_TALK_HUMAN:
        return ("reach_staff", "staff_contact", "talk_to_human", evidence)

    if intent_name in {INTENT_ASK_PRICE, INTENT_ASK_PRODUCT}:
        topic = "promotion_inquiry" if primary_goal == "price_inquiry" else "catalog_inquiry"
        return ("product_inquiry", topic, "learn_product_or_pricing", evidence)

    if intent_name == INTENT_START_ORDER:
        return ("start_order", "ordering", "place_order", evidence)

    if intent_name == INTENT_TRACK_ORDER:
        return ("track_order", "shipment_status", "track_order", evidence)

    if intent_name == INTENT_PAY_NOW:
        return ("payment_action", "payment", "complete_payment", evidence)

    if is_pure_social and shc_category in {"gratitude", "appreciation", "compliment", "dua", "blessing"}:
        return ("social_gratitude", shc_category, "acknowledge_gratitude", evidence)

    if primary_goal and primary_goal not in {"general", "greeting_only"}:
        return (
            primary_goal,
            primary_goal,
            primary_goal,
            evidence,
        )

    return ("general_inquiry", "general", "get_help", evidence)


def _derive_conflicts(
    *,
    state: MerchantConversationState,
    state_relevance: Any,
    current_intent: str,
    active_objective: Optional[str],
) -> Tuple[Tuple[StateConflict, ...], Tuple[str, ...], bool]:
    conflicts: List[StateConflict] = []
    suspend_scope: List[str] = []

    if not _has_active_checkout_workflow(state):
        return tuple(conflicts), tuple(suspend_scope), False

    sr = state_relevance
    topic_shift = bool(getattr(sr, "detected_topic_shift", False))
    support_shift = bool(getattr(sr, "support_listing_topic_shift", False))
    product_info_shift = bool(getattr(sr, "product_information_topic_shift", False))
    safe_to_resume = bool(getattr(sr, "safe_to_resume_state", True))

    commerce_intents = frozenset({
        "complaint_refund",
        "social_interaction",
        "social_gratitude",
        "product_inquiry",
        "general_inquiry",
        "reach_staff",
    })

    if current_intent in {"complaint_refund", "social_gratitude", "social_interaction"}:
        conflicts.append(StateConflict(
            state_field="order_prep",
            persisted_objective=active_objective or "active_checkout",
            conflict_reason=f"current_intent={current_intent}_not_checkout_continuation",
            severity="hard",
        ))
        suspend_scope.extend(["order_prep", "last_question_asked", "stage"])

    elif current_intent == "product_inquiry" and active_objective:
        conflicts.append(StateConflict(
            state_field="order_prep",
            persisted_objective=active_objective,
            conflict_reason="current_turn_is_discovery_not_checkout_slot_fill",
            severity="hard",
        ))
        suspend_scope.extend(["order_prep", "last_question_asked"])

    elif topic_shift or support_shift or product_info_shift:
        conflicts.append(StateConflict(
            state_field="workflow_state",
            persisted_objective=active_objective or "active_workflow",
            conflict_reason="state_relevance_detected_topic_shift",
            severity="hard" if topic_shift else "soft",
        ))
        suspend_scope.append("last_question_asked")

    elif not safe_to_resume and active_objective:
        conflicts.append(StateConflict(
            state_field="workflow_state",
            persisted_objective=active_objective,
            conflict_reason="state_relevance_not_safe_to_resume",
            severity="soft",
        ))

    should_suspend = bool(
        conflicts
        and any(c.severity == "hard" for c in conflicts)
        and current_intent in commerce_intents
    )

    if should_suspend and "order_prep" not in suspend_scope:
        suspend_scope.append("order_prep")

    return tuple(conflicts), tuple(dict.fromkeys(suspend_scope)), should_suspend


def synthesize_turn_understanding(ctx: BrainContext) -> TurnUnderstanding:
    """Build TurnUnderstanding from existing BrainContext signals (read-only)."""
    intent = ctx.intent
    intent_name = str(getattr(intent, "name", "") or "")
    intent_confidence = float(getattr(intent, "confidence", 0.5) or 0.5)

    priority = ctx.intent_priority
    primary_goal = str(getattr(priority, "primary_customer_goal", "") or "")

    state_rel = ctx.state_relevance
    shc = ctx.social_human_context
    state = ctx.state

    evidence: List[UnderstandingEvidence] = []

    current_intent, current_topic, customer_goal, sem_evidence = _derive_semantics(
        intent_name=intent_name,
        intent_confidence=intent_confidence,
        primary_goal=primary_goal,
        shc=shc,
        intent_priority=priority,
    )
    evidence.extend(sem_evidence)

    if state_rel is not None:
        evidence.append(_evidence(
            "state_relevance",
            "state_relevance",
            "verdict",
            (
                f"safe_to_resume={getattr(state_rel, 'safe_to_resume_state', True)} "
                f"topic_shift={getattr(state_rel, 'detected_topic_shift', False)} "
                f"workflows={list(getattr(state_rel, 'active_workflows', []) or [])[:3]}"
            ),
            weight=float(getattr(state_rel, "relevance_confidence", 0.5) or 0.5),
        ))

    if ctx.semantic_interpretation is not None:
        si = ctx.semantic_interpretation
        evidence.append(_evidence(
            "semantic_repair",
            "semantic_turn_interpreter",
            str(getattr(si, "interpreted_intent", "") or ""),
            str(getattr(si, "override_reason", "") or getattr(si, "interpreted_intent", "")),
            weight=float(getattr(si, "confidence", 0.5) or 0.5),
        ))

    active_objective = _derive_active_objective_candidate(state, state_rel)
    if active_objective:
        evidence.append(_evidence(
            "persisted_workflow",
            "conversation_state",
            active_objective,
            f"active_objective_candidate={active_objective}",
            weight=0.4,
        ))

    conflicts, suspend_scope, should_suspend = _derive_conflicts(
        state=state,
        state_relevance=state_rel,
        current_intent=current_intent,
        active_objective=active_objective,
    )

    confidence = intent_confidence
    if priority is not None and primary_goal and primary_goal != "general":
        confidence = max(confidence, 0.75)
    if conflicts:
        confidence = max(confidence, 0.8)

    return TurnUnderstanding(
        current_intent=current_intent,
        current_topic=current_topic,
        customer_goal=customer_goal,
        active_objective_candidate=active_objective,
        evidence=tuple(evidence),
        confidence=min(1.0, confidence),
        conflicts_with_state=conflicts,
        should_suspend_stale_state=should_suspend,
        suspend_scope=suspend_scope,
    )


__all__ = ["synthesize_turn_understanding"]
