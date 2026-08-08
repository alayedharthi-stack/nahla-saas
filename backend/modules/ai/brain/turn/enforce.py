"""
turn/enforce.py
───────────────
Phase 2A — limited Turn Arbiter enforce (platform-wide behind env flag).

Overrides legacy ``Decision`` only when shadow detects an eligible
``mismatch_type``. Routes enforced turns through LLM compose with OwnerBrief.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ..decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS
from ..types import BrainContext, Decision
from .contract import OWNER_DISCOVERY, TurnArbitration, TurnUnderstanding
from .flags import (
    get_enforce_mismatch_types,
    is_enforce_tenant,
    is_turn_arbiter_enforce_enabled,
)
from .legacy_owner import legacy_owner_from_decision
from .mismatch import MISMATCH_CHECKOUT_VS_DISCOVERY, MISMATCH_NONE, classify_owner_mismatch
from .owner_brief import topic_for_owner
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


def _apply_suspend_scope(
    ctx: BrainContext,
    understanding: TurnUnderstanding,
    *,
    mismatch_type: str = "",
) -> None:
    if not understanding.should_suspend_stale_state:
        return
    state = ctx.state
    scope = set(understanding.suspend_scope or ())
    try:
        if "order_prep" in scope or understanding.should_suspend_stale_state:
            use_identity_suspend = (
                mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
                or understanding.current_intent == "product_inquiry"
            )
            if use_identity_suspend:
                from ..commerce.state_continuity_identity import (  # noqa: PLC0415
                    suspend_checkout_authority_retain_identity,
                )

                suspend_checkout_authority_retain_identity(
                    state,
                    reason="turn_arbiter_enforce_suspend",
                )
            else:
                from ..commerce.conversation_context_reset import (  # noqa: PLC0415
                    clear_active_order_context,
                )

                clear_active_order_context(state, reason="turn_arbiter_enforce_suspend")
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — stale checkout suspend is best-effort
        logger.debug(
            "[TURN_ARBITER_ENFORCE] checkout_suspend failed tenant=%s err=%s",
            ctx.tenant_id,
            exc,
        )
    if "last_question_asked" in scope or understanding.should_suspend_stale_state:
        try:
            state.last_question_asked = ""
            state.last_question_answered = True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch is best-effort
            pass


def _identity_hint_from_ctx(ctx: BrainContext) -> Optional[dict[str, Any]]:
    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None)
    if not isinstance(focus, dict):
        return None
    slim: dict[str, Any] = {}
    for key in ("id", "external_id", "title"):
        val = focus.get(key)
        if val is not None and str(val).strip():
            slim[key] = str(val).strip()
    if slim.get("id") or slim.get("external_id"):
        return slim
    return None


def _decision_for_arbitration(
    arbitration: TurnArbitration,
    understanding: TurnUnderstanding,
    *,
    mismatch_type: str,
    ctx: Optional[BrainContext] = None,
) -> Decision:
    """Build enforced decision with OwnerBrief — discovery paths may re-resolve catalog."""
    brief = arbitration.owner_brief
    brief_dict = brief.to_dict()
    owner = arbitration.turn_owner

    args: dict[str, Any] = {
        "turn_arbiter_enforced": True,
        "turn_arbiter_mismatch_type": mismatch_type,
        "turn_owner": owner,
        "owner_brief": brief_dict,
        "compose_mode": brief.compose_mode,
        "response_goal": brief.reply_goal,
        "topic": topic_for_owner(owner),
        "block_order_flow": owner not in {"checkout", "ordering", "payment"},
    }

    if owner in {"support", "post_purchase", "persona/social"}:
        args["block_commerce_escalation"] = True

    action = ACTION_LLM_REPLY
    if (
        owner == OWNER_DISCOVERY
        and mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        and ctx is not None
    ):
        identity = _identity_hint_from_ctx(ctx)
        msg = str(getattr(ctx, "raw_message", None) or ctx.message or "")
        # Fresh catalog retrieval only when a trusted identity hint exists
        # (nominate → re-resolve). Pure coupon/social discovery without a
        # referent stays ACTION_LLM_REPLY + OwnerBrief.
        if identity:
            action = ACTION_SEARCH_PRODUCTS
            args = {
                **args,
                "query": msg,
                "source": "state_continuity_reresolve",
                "block_order_flow": True,
            }
            if identity.get("id"):
                args["product_id"] = identity["id"]
            if identity.get("external_id"):
                args["external_id"] = identity["external_id"]
        else:
            intent_name = str(
                getattr(getattr(ctx, "intent", None), "name", "") or ""
            ).strip().lower()
            # Product catalog talk without a retained id → query search.
            # Price/coupon/promotion asks without identity stay OwnerBrief LLM.
            if intent_name == "ask_product" and msg.strip():
                action = ACTION_SEARCH_PRODUCTS
                args = {
                    **args,
                    "query": msg,
                    "source": "state_continuity_query_fallback",
                    "block_order_flow": True,
                }

    return Decision(
        action=action,
        args=args,
        reason=f"turn_arbiter_enforce:{mismatch_type}",
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

    if mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY:
        try:
            from ..commerce.selection_context import (  # noqa: PLC0415
                verify_structured_unique_selection_against_state,
            )

            if verify_structured_unique_selection_against_state(
                decision,
                getattr(ctx, "state", None),
            ):
                return decision, noop
        except Exception:  # noqa: BLE001  # noqa: silent-ok — verified selection carve-out must not block enforce
            pass

    legacy_action = str(getattr(decision, "action", "") or "")
    legacy_owner = legacy_owner_from_decision(decision)
    proposed_owner = arbitration.turn_owner

    _apply_suspend_scope(ctx, understanding, mismatch_type=mismatch_type)
    enforced_decision = _decision_for_arbitration(
        arbitration,
        understanding,
        mismatch_type=mismatch_type,
        ctx=ctx,
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
        "reply_goal=%s compose_mode=%s suspend_stale=%s preview=%r",
        ctx.tenant_id,
        mismatch_type,
        proposed_owner,
        legacy_owner,
        legacy_action,
        enforced_decision.action,
        arbitration.owner_brief.reply_goal,
        arbitration.owner_brief.compose_mode,
        str(understanding.should_suspend_stale_state).lower(),
        (getattr(ctx, "raw_message", None) or ctx.message or "")[:80],
    )
    try:
        from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
            catalog_resilience_known_facts,
        )
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        _meta = {}
        _profile = getattr(ctx, "profile", None)
        if isinstance(_profile, dict):
            _meta = dict(_profile.get("inbound_metadata") or {})
        _msg = str(getattr(ctx, "raw_message", None) or ctx.message or "")
        if is_catalog_order_inbound(_meta, _msg):
            _facts = catalog_resilience_known_facts(
                inbound_metadata=_meta,
                message=_msg,
                state=getattr(ctx, "state", None),
            )
            if _facts.get("catalog_order_current_turn") or _facts.get("line_items_known"):
                logger.warning(
                    "[TURN_ARBITER_CATALOG] tenant=%s mismatch_type=%s "
                    "legacy_action=%s new_action=%s catalog_order=true line_items_known=%s",
                    ctx.tenant_id,
                    mismatch_type,
                    legacy_action,
                    enforced_decision.action,
                    str(bool(_facts.get("line_items_known"))).lower(),
                )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog arbiter telemetry must not block enforce
        pass

    ctx.turn_enforce_result = result  # type: ignore[attr-defined]
    ctx.turn_legacy_decision = decision  # type: ignore[attr-defined]
    return enforced_decision, result


__all__ = ["TurnEnforceResult", "maybe_enforce_turn_decision"]
