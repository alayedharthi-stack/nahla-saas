"""
turn/arbiter.py
───────────────
Phase 1 — deterministic Turn Arbiter over structured TurnUnderstanding.

Selects exactly one turn owner and attaches OwnerBrief for compose.
"""
from __future__ import annotations

from typing import Any, Optional

from ..types import BrainContext
from .contract import (
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_HEALTH_ADVISORY,
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
from .owner_brief import build_owner_brief

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
    if getattr(sr, "product_information_topic_shift", False):
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


def _make_arbitration(
    owner: str,
    reason: str,
    understanding: TurnUnderstanding,
    ctx: BrainContext,
    *,
    confidence: Optional[float] = None,
    slot_replay_approved: bool = False,
    approved_proposal: Optional[str] = None,
) -> TurnArbitration:
    return TurnArbitration(
        turn_owner=owner,
        reason=reason,
        confidence=float(confidence if confidence is not None else understanding.confidence),
        owner_brief=build_owner_brief(
            owner,
            understanding,
            ctx,
            slot_replay_approved=slot_replay_approved,
        ),
        slot_replay_approved=slot_replay_approved,
        approved_proposal=approved_proposal,
    )


def arbitrate_turn(
    understanding: TurnUnderstanding,
    ctx: BrainContext,
) -> TurnArbitration:
    """Select exactly one turn owner from structured understanding."""
    state = ctx.state
    state_rel = ctx.state_relevance
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")

    if understanding.should_suspend_stale_state:
        if understanding.current_intent == "complaint_refund":
            return _make_arbitration(
                OWNER_SUPPORT,
                "complaint_with_stale_checkout_suspended",
                understanding,
                ctx,
            )
        if understanding.current_intent in {"social_gratitude", "social_interaction"}:
            owner = (
                OWNER_POST_PURCHASE
                if _post_purchase_window(ctx)
                else OWNER_PERSONA_SOCIAL
            )
            return _make_arbitration(
                owner,
                "social_turn_stale_checkout_suspended",
                understanding,
                ctx,
            )
        if understanding.current_intent == "product_inquiry":
            return _make_arbitration(
                OWNER_DISCOVERY,
                "discovery_turn_stale_checkout_suspended",
                understanding,
                ctx,
            )

    if understanding.current_intent == "complaint_refund":
        owner = OWNER_POST_PURCHASE if _post_purchase_window(ctx) else OWNER_SUPPORT
        return _make_arbitration(
            owner,
            "complaint_refund_intent",
            understanding,
            ctx,
        )

    if understanding.current_intent in {"social_gratitude", "social_interaction"}:
        owner = (
            OWNER_POST_PURCHASE
            if _post_purchase_window(ctx) and understanding.current_intent == "social_gratitude"
            else OWNER_PERSONA_SOCIAL
        )
        return _make_arbitration(
            owner,
            f"{understanding.current_intent}_detected",
            understanding,
            ctx,
        )

    if understanding.current_intent == "product_inquiry":
        return _make_arbitration(
            OWNER_DISCOVERY,
            "product_or_promotion_inquiry",
            understanding,
            ctx,
        )

    if understanding.current_intent == "track_order":
        return _make_arbitration(
            OWNER_TRACKING,
            "track_order_intent",
            understanding,
            ctx,
        )

    if understanding.current_intent == "health_advisory":
        return _make_arbitration(
            OWNER_HEALTH_ADVISORY,
            "health_advisory_solution_seeking",
            understanding,
            ctx,
        )

    if understanding.current_intent == "start_order":
        return _make_arbitration(
            OWNER_ORDERING,
            "explicit_start_order",
            understanding,
            ctx,
        )

    if understanding.current_intent == "checkout_continuation":
        owner = OWNER_CHECKOUT
        if str(getattr(state, "stage", "") or "") == "ordering":
            owner = OWNER_ORDERING
        return _make_arbitration(
            owner,
            "checkout_continuation_current_turn",
            understanding,
            ctx,
            slot_replay_approved=bool(understanding.active_objective_candidate),
            approved_proposal=understanding.active_objective_candidate,
        )

    if _payment_aligned(state, state_rel, understanding):
        return _make_arbitration(
            OWNER_PAYMENT,
            "payment_flow_aligned",
            understanding,
            ctx,
            slot_replay_approved=True,
            approved_proposal=understanding.active_objective_candidate,
        )

    if _checkout_aligned(state_rel, understanding):
        owner = OWNER_CHECKOUT
        if str(getattr(state, "stage", "") or "") == "ordering":
            owner = OWNER_ORDERING
        return _make_arbitration(
            owner,
            "checkout_continuation_approved",
            understanding,
            ctx,
            slot_replay_approved=True,
            approved_proposal=understanding.active_objective_candidate,
        )

    if understanding.current_intent == "reach_staff":
        shc = ctx.social_human_context
        is_pure_social = bool(getattr(shc, "is_pure_social_turn", False))
        high_trust = (
            understanding.confidence >= _STAFF_ESCALATION_MIN_CONFIDENCE
            and not is_pure_social
            and intent_name == "talk_to_human"
        )
        if high_trust:
            return _make_arbitration(
                OWNER_STAFF_ESCALATION,
                "explicit_staff_request_high_trust",
                understanding,
                ctx,
            )
        return _make_arbitration(
            OWNER_SUPPORT,
            "staff_request_low_trust_routed_to_support",
            understanding,
            ctx,
        )

    if understanding.current_intent == "payment_action":
        return _make_arbitration(
            OWNER_PAYMENT,
            "payment_action_intent",
            understanding,
            ctx,
        )

    return _make_arbitration(
        OWNER_PERSONA_SOCIAL,
        "default_persona_social",
        understanding,
        ctx,
        confidence=max(0.5, understanding.confidence * 0.8),
    )


__all__ = ["arbitrate_turn"]
