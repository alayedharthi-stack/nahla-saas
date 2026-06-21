"""
turn/arbiter.py
───────────────
Phase 1 — deterministic Turn Arbiter over structured TurnUnderstanding.

Selects exactly one turn owner. Does not mutate state or drive replies.
"""
from __future__ import annotations

from typing import Any, Optional

from ..types import BrainContext
from .contract import (
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_ORDERING,
    OWNER_PAYMENT,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
    OWNER_TRACKING,
    TurnArbitration,
    TurnUnderstanding,
)

_STAFF_ESCALATION_MIN_CONFIDENCE = 0.85


def _post_purchase_window(ctx: BrainContext) -> bool:
    bundle = dict(getattr(ctx, "commerce_bundle", None) or {})
    if bundle.get("delivered") or bundle.get("post_purchase_window"):
        return True
    rel = getattr(ctx, "relational_state", None)
    if rel is not None and getattr(rel, "post_purchase_window", False):
        return True
    return False


def _checkout_aligned(state_relevance: Any, understanding: TurnUnderstanding) -> bool:
    if understanding.should_suspend_stale_state:
        return False
    if not understanding.active_objective_candidate:
        return False
    sr = state_relevance
    if sr is None:
        return False
    if getattr(sr, "detected_topic_shift", False):
        return False
    if not getattr(sr, "safe_to_resume_state", True):
        return False
    if understanding.current_intent in {
        "complaint_refund",
        "social_gratitude",
        "social_interaction",
        "product_inquiry",
    }:
        return False
    return True


def _payment_aligned(state: Any, state_relevance: Any, understanding: TurnUnderstanding) -> bool:
    if understanding.should_suspend_stale_state:
        return False
    op = getattr(state, "order_prep", None)
    if op is None:
        return False
    if not getattr(op, "awaiting_payment_receipt", False):
        return False
    sr = state_relevance
    if sr is not None and getattr(sr, "payment_state_relevant", False):
        return True
    return understanding.current_intent == "payment_action"


def arbitrate_turn(
    understanding: TurnUnderstanding,
    ctx: BrainContext,
) -> TurnArbitration:
    """Select exactly one turn owner from structured understanding."""
    state = ctx.state
    state_rel = ctx.state_relevance
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")

    # ── Phase A: stale state suspension blocks checkout/payment continuation ──
    if understanding.should_suspend_stale_state:
        if understanding.current_intent == "complaint_refund":
            return TurnArbitration(
                turn_owner=OWNER_SUPPORT,
                reason="complaint_with_stale_checkout_suspended",
                confidence=understanding.confidence,
            )
        if understanding.current_intent in {"social_gratitude", "social_interaction"}:
            owner = (
                OWNER_POST_PURCHASE
                if _post_purchase_window(ctx)
                else OWNER_PERSONA_SOCIAL
            )
            return TurnArbitration(
                turn_owner=owner,
                reason="social_turn_stale_checkout_suspended",
                confidence=understanding.confidence,
            )
        if understanding.current_intent == "product_inquiry":
            return TurnArbitration(
                turn_owner=OWNER_DISCOVERY,
                reason="discovery_turn_stale_checkout_suspended",
                confidence=understanding.confidence,
            )

    # ── Phase B: hard routing from understanding ──
    if understanding.current_intent == "complaint_refund":
        owner = OWNER_POST_PURCHASE if _post_purchase_window(ctx) else OWNER_SUPPORT
        return TurnArbitration(
            turn_owner=owner,
            reason="complaint_refund_intent",
            confidence=understanding.confidence,
        )

    if understanding.current_intent in {"social_gratitude", "social_interaction"}:
        owner = (
            OWNER_POST_PURCHASE
            if _post_purchase_window(ctx) and understanding.current_intent == "social_gratitude"
            else OWNER_PERSONA_SOCIAL
        )
        return TurnArbitration(
            turn_owner=owner,
            reason=f"{understanding.current_intent}_detected",
            confidence=understanding.confidence,
        )

    if understanding.current_intent == "product_inquiry":
        return TurnArbitration(
            turn_owner=OWNER_DISCOVERY,
            reason="product_or_promotion_inquiry",
            confidence=understanding.confidence,
        )

    if understanding.current_intent == "track_order":
        return TurnArbitration(
            turn_owner=OWNER_TRACKING,
            reason="track_order_intent",
            confidence=understanding.confidence,
        )

    if understanding.current_intent == "start_order":
        return TurnArbitration(
            turn_owner=OWNER_ORDERING,
            reason="explicit_start_order",
            confidence=understanding.confidence,
        )

    if _payment_aligned(state, state_rel, understanding):
        return TurnArbitration(
            turn_owner=OWNER_PAYMENT,
            reason="payment_flow_aligned",
            confidence=understanding.confidence,
            slot_replay_approved=True,
            approved_proposal=understanding.active_objective_candidate,
        )

    if _checkout_aligned(state_rel, understanding):
        owner = OWNER_CHECKOUT
        if understanding.active_objective_candidate and "collect" in understanding.active_objective_candidate:
            owner = OWNER_CHECKOUT
        elif str(getattr(state, "stage", "") or "") == "ordering":
            owner = OWNER_ORDERING
        return TurnArbitration(
            turn_owner=owner,
            reason="checkout_continuation_approved",
            confidence=understanding.confidence,
            slot_replay_approved=True,
            approved_proposal=understanding.active_objective_candidate,
        )

    # Staff escalation — high-trust only (not keyword-only).
    if understanding.current_intent == "reach_staff":
        shc = ctx.social_human_context
        is_pure_social = bool(getattr(shc, "is_pure_social_turn", False))
        high_trust = (
            understanding.confidence >= _STAFF_ESCALATION_MIN_CONFIDENCE
            and not is_pure_social
            and intent_name == "talk_to_human"
        )
        if high_trust:
            return TurnArbitration(
                turn_owner=OWNER_STAFF_ESCALATION,
                reason="explicit_staff_request_high_trust",
                confidence=understanding.confidence,
            )
        return TurnArbitration(
            turn_owner=OWNER_SUPPORT,
            reason="staff_request_low_trust_routed_to_support",
            confidence=understanding.confidence,
        )

    if understanding.current_intent == "payment_action":
        return TurnArbitration(
            turn_owner=OWNER_PAYMENT,
            reason="payment_action_intent",
            confidence=understanding.confidence,
        )

    # Default — persona/social for general/social turns.
    return TurnArbitration(
        turn_owner=OWNER_PERSONA_SOCIAL,
        reason="default_persona_social",
        confidence=max(0.5, understanding.confidence * 0.8),
    )


__all__ = ["arbitrate_turn"]
