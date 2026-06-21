"""
turn/enforce.py
───────────────
Phase 2A — limited Turn Arbiter enforce for allowlisted tenants.

Overrides legacy ``Decision`` only when shadow detects an eligible
``mismatch_type``. Platform-wide logic; tenant scope via env allowlist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ..decision.actions import (
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
    ACTION_SUGGEST_COUPON,
)
from ..types import BrainContext, Decision
from .contract import (
    OWNER_DISCOVERY,
    OWNER_PERSONA_SOCIAL,
    OWNER_POST_PURCHASE,
    OWNER_SUPPORT,
    TurnArbitration,
    TurnUnderstanding,
)
from .flags import (
    get_enforce_mismatch_types,
    is_enforce_tenant,
    is_turn_arbiter_enforce_enabled,
)
from .legacy_owner import legacy_owner_from_decision
from .mismatch import MISMATCH_NONE, classify_owner_mismatch
from .telemetry import build_shadow_telemetry

logger = logging.getLogger("nahla.brain.turn_enforce")


@dataclass(frozen=True)
class TurnEnforceResult:
    enforced: bool
    mismatch_type: str = MISMATCH_NONE
    proposed_owner: str = ""
    legacy_owner: str = ""
    legacy_action: str = ""
    new_action: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enforced": self.enforced,
            "mismatch_type": self.mismatch_type,
            "proposed_owner": self.proposed_owner,
            "legacy_owner": self.legacy_owner,
            "legacy_action": self.legacy_action,
            "new_action": self.new_action,
            "reason": self.reason,
        }


def _apply_suspend_scope(ctx: BrainContext, understanding: TurnUnderstanding) -> None:
    if not understanding.should_suspend_stale_state:
        return
    state = ctx.state
    scope = set(understanding.suspend_scope or ())
    try:
        if "order_prep" in scope or understanding.should_suspend_stale_state:
            from ..commerce.conversation_context_reset import (  # noqa: PLC0415
                clear_active_order_context,
            )

            clear_active_order_context(state, reason="turn_arbiter_enforce_suspend")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[TURN_ARBITER_ENFORCE] clear_active_order_context failed tenant=%s err=%s",
            ctx.tenant_id,
            exc,
        )
    if "last_question_asked" in scope or understanding.should_suspend_stale_state:
        try:
            state.last_question_asked = ""
            state.last_question_answered = True
        except Exception:  # noqa: BLE001
            pass


def _decision_for_owner(
    owner: str,
    ctx: BrainContext,
    understanding: TurnUnderstanding,
    *,
    mismatch_type: str,
) -> Decision:
    base_args: dict[str, Any] = {
        "turn_arbiter_enforced": True,
        "turn_arbiter_mismatch_type": mismatch_type,
        "block_order_flow": True,
    }

    if owner in {OWNER_SUPPORT, OWNER_POST_PURCHASE}:
        try:
            from ..commerce.complaint_refund_topic_guard import (  # noqa: PLC0415
                try_complaint_refund_decision,
            )

            complaint = try_complaint_refund_decision(ctx)
            if complaint is not None:
                args = dict(complaint.args or {})
                args["turn_arbiter_enforced"] = True
                args["turn_arbiter_mismatch_type"] = mismatch_type
                return Decision(
                    action=complaint.action,
                    args=args,
                    reason=f"turn_arbiter_enforce:{mismatch_type}",
                    confidence=complaint.confidence,
                )
        except Exception:  # noqa: BLE001
            pass
        topic = (
            "post_purchase_support"
            if owner == OWNER_POST_PURCHASE
            else "support_complaint_refund"
        )
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                **base_args,
                "topic": topic,
                "block_commerce_escalation": True,
            },
            reason=f"turn_arbiter_enforce:{mismatch_type}",
            confidence=understanding.confidence,
        )

    if owner == OWNER_DISCOVERY:
        facts = ctx.facts
        if getattr(facts, "has_coupons", False) and understanding.current_topic == "promotion_inquiry":
            return Decision(
                action=ACTION_SUGGEST_COUPON,
                args={**base_args, "topic": "discovery_coupon"},
                reason=f"turn_arbiter_enforce:{mismatch_type}",
                confidence=understanding.confidence,
            )
        if understanding.current_intent == "product_inquiry":
            return Decision(
                action=ACTION_SEARCH_PRODUCTS,
                args={**base_args, "topic": "discovery"},
                reason=f"turn_arbiter_enforce:{mismatch_type}",
                confidence=understanding.confidence,
            )
        return Decision(
            action=ACTION_FAQ_REPLY,
            args={**base_args, "topic": "discovery_faq"},
            reason=f"turn_arbiter_enforce:{mismatch_type}",
            confidence=understanding.confidence,
        )

    if owner == OWNER_PERSONA_SOCIAL:
        return Decision(
            action=ACTION_SOCIAL_REPLY,
            args={
                **base_args,
                "topic": understanding.current_topic or "social_gratitude",
                "block_commerce_escalation": True,
            },
            reason=f"turn_arbiter_enforce:{mismatch_type}",
            confidence=understanding.confidence,
        )

    return Decision(
        action=ACTION_LLM_REPLY,
        args={**base_args, "topic": owner.replace("/", "_")},
        reason=f"turn_arbiter_enforce:{mismatch_type}:fallback",
        confidence=understanding.confidence,
    )


def maybe_enforce_turn_decision(
    ctx: BrainContext,
    decision: Decision,
) -> Tuple[Decision, TurnEnforceResult]:
    """
    Optionally replace legacy decision when arbiter mismatch is enforceable.

    Returns ``(decision, enforce_result)``. When not enforced, ``decision`` is
    unchanged and ``enforce_result.enforced`` is False.
    """
    noop = TurnEnforceResult(enforced=False)

    if not is_turn_arbiter_enforce_enabled():
        return decision, noop
    if not is_enforce_tenant(getattr(ctx, "tenant_id", None)):
        return decision, noop

    understanding: Optional[TurnUnderstanding] = getattr(
        ctx, "turn_understanding_shadow", None,
    )
    arbitration: Optional[TurnArbitration] = getattr(
        ctx, "turn_arbitration_shadow", None,
    )
    if understanding is None or arbitration is None:
        return decision, noop

    telemetry = build_shadow_telemetry(understanding, arbitration, decision)
    if not telemetry.owner_mismatch:
        return decision, noop

    mismatch_type = telemetry.mismatch_type
    if mismatch_type not in get_enforce_mismatch_types():
        return decision, noop

    legacy_action = str(getattr(decision, "action", "") or "")
    legacy_owner = legacy_owner_from_decision(decision)
    proposed_owner = arbitration.turn_owner

    _apply_suspend_scope(ctx, understanding)
    enforced_decision = _decision_for_owner(
        proposed_owner,
        ctx,
        understanding,
        mismatch_type=mismatch_type,
    )

    result = TurnEnforceResult(
        enforced=True,
        mismatch_type=mismatch_type,
        proposed_owner=proposed_owner,
        legacy_owner=legacy_owner,
        legacy_action=legacy_action,
        new_action=str(enforced_decision.action),
        reason=enforced_decision.reason,
    )

    logger.info(
        "[TURN_ARBITER_ENFORCE] tenant=%s enforced=true mismatch_type=%s "
        "proposed_owner=%s legacy_owner=%s legacy_action=%s new_action=%s "
        "suspend_stale=%s preview=%r",
        ctx.tenant_id,
        mismatch_type,
        proposed_owner,
        legacy_owner,
        legacy_action,
        enforced_decision.action,
        str(understanding.should_suspend_stale_state).lower(),
        (getattr(ctx, "raw_message", None) or ctx.message or "")[:80],
    )

    ctx.turn_enforce_result = result  # type: ignore[attr-defined]
    ctx.turn_legacy_decision = decision  # type: ignore[attr-defined]
    return enforced_decision, result


__all__ = ["TurnEnforceResult", "maybe_enforce_turn_decision"]
