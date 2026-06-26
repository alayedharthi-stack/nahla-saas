"""
turn/understanding.py
─────────────────────
Phase 1/3B — synthesize TurnUnderstanding from existing pipeline signals.

Current-turn message has highest authority. Persisted workflow state is
evidence, not automatic owner. Read-only: no new regex patterns.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from ..types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_COMPLAINT_REFUND,
    INTENT_GREETING,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
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
_SEMANTIC_AUTHORITY_MIN_CONFIDENCE = 0.72

# Semantic interpreter intents → turn understanding (current-turn authority).
_SEMANTIC_TO_UNDERSTANDING = {
    "fulfillment_location_update": (
        "checkout_continuation",
        "fulfillment",
        "provide_delivery_location",
    ),
    "select_list_option": (
        "checkout_continuation",
        "option_selection",
        "select_listed_option",
    ),
    "show_all_variants_or_prices": (
        "product_inquiry",
        "catalog_inquiry",
        "learn_product_or_pricing",
    ),
    "ask_price_specific_variant": (
        "product_inquiry",
        "catalog_inquiry",
        "learn_product_or_pricing",
    ),
    "refer_last_product": (
        "product_inquiry",
        "catalog_inquiry",
        "refer_last_product",
    ),
    "clarify_variants_natural": (
        "product_inquiry",
        "catalog_inquiry",
        "clarify_variants",
    ),
}


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

    if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE:
        return ("health_advisory", "health_inquiry", "advisory_product_guidance", evidence)

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


def _apply_operational_turn_signals(
    ctx: BrainContext,
    *,
    intent_name: str,
    evidence: List[UnderstandingEvidence],
) -> Optional[Tuple[str, str, str, List[UnderstandingEvidence]]]:
    """
    Current-turn operational signals from existing classifiers (not phrase lists).

    Returns (current_intent, topic, customer_goal, evidence) or None.
    """
    msg = ctx.message or ""
    state = ctx.state
    order_prep = getattr(state, "order_prep", None)

    try:
        from ..commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            is_bare_city_token_message,
            message_fulfills_checkout_slot,
        )

        if message_fulfills_checkout_slot(msg, order_prep=order_prep) or is_bare_city_token_message(msg):
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "checkout_slot_guard",
                "checkout_slot_fulfillment",
                "message fulfills awaited checkout slot",
                weight=0.92,
            ))
            return (
                "checkout_continuation",
                "fulfillment",
                "provide_checkout_slot_answer",
                evidence,
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        from core.catalog_authoritative_line_items import is_shipping_address_capture_context  # noqa: PLC0415

        if is_shipping_address_capture_context(
            msg,
            order_prep=order_prep,
            stage=str(getattr(state, "stage", "") or ""),
        ):
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "shipping_address_context",
                "address_capture",
                "shipping address capture context",
                weight=0.9,
            ))
            return (
                "checkout_continuation",
                "fulfillment",
                "provide_delivery_address",
                evidence,
            )
    except Exception:  # noqa: BLE001
        pass

    _inbound_meta: dict = {}
    profile = getattr(ctx, "profile", None)
    if isinstance(profile, dict):
        _inbound_meta = dict(profile.get("inbound_metadata") or {})

    try:
        from ..commerce.current_order_amount import (  # noqa: PLC0415
            should_route_current_order_amount_over_tracking,
        )

        if should_route_current_order_amount_over_tracking(
            msg,
            state=state,
            inbound_metadata=_inbound_meta,
        ):
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "current_order_amount",
                "payment_amount_inquiry",
                "active checkout amount question",
                weight=0.93,
            ))
            return ("payment_action", "payment", "current_order_amount", evidence)
    except Exception:  # noqa: BLE001
        pass

    try:
        from ..commerce.contact_route_policy import has_explicit_contact_intent  # noqa: PLC0415

        if has_explicit_contact_intent(msg) or intent_name in {
            INTENT_TALK_HUMAN,
            INTENT_ASK_OWNER_CONTACT,
        }:
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "contact_route_policy",
                "staff_contact",
                "explicit staff/contact request",
                weight=0.91,
            ))
            return ("reach_staff", "staff_contact", "talk_to_human", evidence)
    except Exception:  # noqa: BLE001
        pass

    if intent_name in {INTENT_ASK_PAYMENT_INFO, INTENT_PAY_NOW}:
        evidence = list(evidence)
        evidence.append(_evidence(
            "current_turn_authority",
            "intent_classifier",
            f"intent.{intent_name}",
            "payment-related intent",
            weight=0.88,
        ))
        return ("payment_action", "payment", "payment_inquiry", evidence)

    try:
        from ..commerce.solution_seeking import classify_solution_seeking_commerce  # noqa: PLC0415

        if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE or classify_solution_seeking_commerce(msg):
            axis = ""
            if intent_name == INTENT_NEED_BASED_PRODUCT_ADVICE:
                axis = "need_based_intent"
            else:
                nb = classify_solution_seeking_commerce(msg)
                axis = nb.axis if nb is not None else "solution_seeking"
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "solution_seeking",
                axis or "health_advisory",
                "solution-seeking / health advisory commerce",
                weight=0.9,
            ))
            return ("health_advisory", "health_inquiry", "advisory_product_guidance", evidence)
    except Exception:  # noqa: BLE001
        pass

    return None


def _apply_current_turn_authority(
    *,
    current_intent: str,
    current_topic: str,
    customer_goal: str,
    evidence: List[UnderstandingEvidence],
    semantic_interpretation: Any,
    state_relevance: Any,
    active_objective: Optional[str],
) -> Tuple[str, str, str, List[UnderstandingEvidence], Optional[str]]:
    """
    Current inbound message wins over stale workflow when semantic evidence is strong.

    Returns possibly updated intent/topic/goal, evidence, and demoted objective.
    """
    if semantic_interpretation is None:
        return current_intent, current_topic, customer_goal, evidence, active_objective

    si_conf = float(getattr(semantic_interpretation, "confidence", 0.0) or 0.0)
    interpreted = str(getattr(semantic_interpretation, "interpreted_intent", "") or "")
    topic_shift = bool(getattr(semantic_interpretation, "topic_shift", False))
    override_social = bool(getattr(semantic_interpretation, "should_override_social", False))

    if si_conf >= _SEMANTIC_AUTHORITY_MIN_CONFIDENCE and interpreted in _SEMANTIC_TO_UNDERSTANDING:
        mapped = _SEMANTIC_TO_UNDERSTANDING[interpreted]
        evidence = list(evidence)
        evidence.append(_evidence(
            "current_turn_authority",
            "semantic_turn_interpreter",
            interpreted,
            f"current_turn_semantic_override confidence={si_conf:.2f}",
            weight=si_conf,
        ))
        demoted_objective = active_objective
        if interpreted == "fulfillment_location_update" and active_objective:
            demoted_objective = active_objective
        elif topic_shift and active_objective:
            demoted_objective = None
        return mapped[0], mapped[1], mapped[2], evidence, demoted_objective

    if override_social and si_conf >= 0.65 and interpreted:
        evidence = list(evidence)
        evidence.append(_evidence(
            "current_turn_authority",
            "semantic_turn_interpreter",
            interpreted,
            "semantic_override_social",
            weight=si_conf,
        ))
        if interpreted in _SEMANTIC_TO_UNDERSTANDING:
            mapped = _SEMANTIC_TO_UNDERSTANDING[interpreted]
            return mapped[0], mapped[1], mapped[2], evidence, active_objective

    if topic_shift and state_relevance is not None:
        if bool(getattr(state_relevance, "detected_topic_shift", False)):
            evidence = list(evidence)
            evidence.append(_evidence(
                "current_turn_authority",
                "state_relevance",
                "topic_shift",
                "current_message_topic_shift_over_stale_workflow",
                weight=0.85,
            ))
            if current_intent in {
                "social_interaction",
                "social_gratitude",
                "product_inquiry",
                "complaint_refund",
            }:
                return current_intent, current_topic, customer_goal, evidence, None

    return current_intent, current_topic, customer_goal, evidence, active_objective


def _derive_conflicts(
    *,
    state: MerchantConversationState,
    state_relevance: Any,
    current_intent: str,
    active_objective: Optional[str],
    message: str = "",
    intent_name: str = "",
    ctx: Any = None,
) -> Tuple[Tuple[StateConflict, ...], Tuple[str, ...], bool]:
    conflicts: List[StateConflict] = []
    suspend_scope: List[str] = []

    if not _has_active_checkout_workflow(state):
        return tuple(conflicts), tuple(suspend_scope), False

    try:
        from .ownership import has_explicit_catalog_browse_intent  # noqa: PLC0415
        from ..catalog.catalog_browse_turn_policy import is_fresh_start_order_turn  # noqa: PLC0415

        if has_explicit_catalog_browse_intent(
            ctx,
            message=message or "",
            intent_name=intent_name,
        ) if ctx is not None else False:
            conflicts.append(StateConflict(
                state_field="order_prep",
                persisted_objective=active_objective or "active_checkout",
                conflict_reason="catalog_browse_turn_isolates_stale_checkout",
                severity="hard",
            ))
            suspend_scope.extend(["order_prep", "last_question_asked", "stage"])
            return tuple(conflicts), tuple(dict.fromkeys(suspend_scope)), True

        if is_fresh_start_order_turn(message or ""):
            conflicts.append(StateConflict(
                state_field="order_prep",
                persisted_objective=active_objective or "active_checkout",
                conflict_reason="fresh_start_order_isolates_stale_checkout",
                severity="hard",
            ))
            suspend_scope.extend(["order_prep", "last_question_asked", "stage"])
            return tuple(conflicts), tuple(dict.fromkeys(suspend_scope)), True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse policy probe
        pass

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

    checkout_continuation = current_intent == "checkout_continuation"

    if checkout_continuation and active_objective:
        return tuple(conflicts), tuple(suspend_scope), False

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

    operational = _apply_operational_turn_signals(
        ctx,
        intent_name=intent_name,
        evidence=evidence,
    )
    if operational is not None:
        current_intent, current_topic, customer_goal, evidence = operational

    active_objective = _derive_active_objective_candidate(state, state_rel)

    try:
        from .ownership import has_explicit_catalog_browse_intent  # noqa: PLC0415

        if has_explicit_catalog_browse_intent(ctx, intent_name=intent_name):
            current_intent = "product_inquiry"
            current_topic = "catalog_inquiry"
            customer_goal = "learn_product_or_pricing"
            evidence.append(_evidence(
                "current_turn_authority",
                "turn_ownership",
                "explicit_catalog_browse",
                "explicit_catalog_browse_turn_overrides_stale_checkout",
                weight=0.9,
            ))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional browse policy probe
        pass

    current_intent, current_topic, customer_goal, evidence, active_objective = (
        _apply_current_turn_authority(
            current_intent=current_intent,
            current_topic=current_topic,
            customer_goal=customer_goal,
            evidence=evidence,
            semantic_interpretation=ctx.semantic_interpretation,
            state_relevance=state_rel,
            active_objective=active_objective,
        )
    )

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

    if active_objective:
        evidence.append(_evidence(
            "persisted_workflow",
            "conversation_state",
            active_objective,
            f"active_objective_candidate={active_objective}",
            weight=0.35,
        ))
    elif _has_active_checkout_workflow(state):
        evidence.append(_evidence(
            "persisted_workflow",
            "conversation_state",
            "stale_checkout_context",
            "checkout_state_present_but_not_current_turn_owner",
            weight=0.25,
        ))

    conflicts, suspend_scope, should_suspend = _derive_conflicts(
        state=state,
        state_relevance=state_rel,
        current_intent=current_intent,
        active_objective=active_objective,
        message=ctx.message or "",
        intent_name=intent_name,
        ctx=ctx,
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
