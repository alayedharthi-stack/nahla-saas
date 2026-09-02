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
from .contract import (
    OWNER_DISCOVERY,
    OWNER_PERSONA_SOCIAL,
    OWNER_STAFF_ESCALATION,
    OWNER_SUPPORT,
    OWNER_TRACKING,
    TurnArbitration,
    TurnUnderstanding,
)
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


def _concrete_product_knowledge_decision(
    ctx: BrainContext,
    *,
    mismatch_type: str,
) -> Optional[Decision]:
    """Reuse the existing fact owner before an enforced discovery fallback.

    A checkout-vs-discovery mismatch must still release stale checkout
    authority.  It must not, however, replace a catalog-confirmed product
    information turn with generic search merely because the legacy decision
    was a checkout action.  This helper is deliberately a consumer of the
    existing canonical-referent and product-knowledge owners: it introduces
    neither phrase classification nor a second fact bundle.
    """
    if mismatch_type != MISMATCH_CHECKOUT_VS_DISCOVERY:
        return None

    understanding = getattr(ctx, "turn_understanding_shadow", None)
    if str(getattr(understanding, "current_intent", "") or "") != "product_inquiry":
        return None

    message = str(getattr(ctx, "raw_message", None) or ctx.message or "").strip()
    if not message:
        return None

    try:
        from ..commerce.catalog_reasoning_evidence import (  # noqa: PLC0415
            ensure_canonical_referent_catalog_projection,
        )
        from ..commerce.commerce_focus_owner import (  # noqa: PLC0415
            canonical_product_referent,
            has_structured_catalog_identity,
            product_focus_identity,
        )
        from ..product_discovery_gate import (  # noqa: PLC0415
            _explicit_category_scope_broadening,
            _turn_scoped_to_canonical_referent,
        )
        from ..postprocess.availability_guard_policy import (  # noqa: PLC0415
            inbound_asks_stock_or_orderability,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — existing owners fail closed
        return None

    referent = canonical_product_referent(getattr(ctx, "state", None))
    if not has_structured_catalog_identity(referent):
        return None
    if (
        inbound_asks_stock_or_orderability(message)
        or _explicit_category_scope_broadening(message)
        or not _turn_scoped_to_canonical_referent(
            message,
            referent,
            state=getattr(ctx, "state", None),
        )
    ):
        return None

    try:
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        profile = getattr(ctx, "profile", None)
        inbound_metadata = (
            dict(profile.get("inbound_metadata") or {})
            if isinstance(profile, dict)
            else {}
        )
        if is_catalog_order_inbound(inbound_metadata, message):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — do not infer checkout on probe failure
        pass

    projected = ensure_canonical_referent_catalog_projection(
        db=getattr(ctx, "_db", None),
        tenant_id=getattr(ctx, "tenant_id", None),
        state=getattr(ctx, "state", None),
        facts=getattr(ctx, "facts", None),
        merchant_context=getattr(ctx, "merchant_context", None),
        bind_to_merchant_context=True,
    )
    if not has_structured_catalog_identity(projected):
        return None

    try:
        from ..commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            TOPIC_PRODUCT_KNOWLEDGE_FACTS,
            try_product_knowledge_decision,
        )

        decision = try_product_knowledge_decision(ctx)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — existing fact owner remains authoritative
        return None

    args = dict(getattr(decision, "args", None) or {}) if decision is not None else {}
    subject = dict(args.get("subject_product") or {})
    if (
        decision is None
        or args.get("topic") != TOPIC_PRODUCT_KNOWLEDGE_FACTS
        or product_focus_identity(subject) != product_focus_identity(projected)
    ):
        return None
    return decision


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
        if identity:
            try:
                from ..commerce.state_continuity_identity import (  # noqa: PLC0415
                    _is_explicit_different_product,
                )

                if _is_explicit_different_product(
                    getattr(ctx, "state", None),
                    getattr(ctx, "intent", None),
                ):
                    identity = None
            except Exception:  # noqa: BLE001  # noqa: silent-ok — preserve safe legacy fallback
                pass
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

    if _should_preserve_authoritative_order_support(ctx, decision, arbitration):
        logger.info(
            "[TURN_ARBITER_ENFORCE] tenant=%s enforced=false "
            "reason=authoritative_order_support_preserved "
            "proposed_owner=%s legacy_owner=%s mismatch_type=%s preview=%r",
            ctx.tenant_id,
            arbitration.turn_owner,
            legacy_owner_from_decision(decision),
            telemetry.mismatch_type,
            (getattr(ctx, "raw_message", None) or ctx.message or "")[:80],
        )
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

    product_knowledge_decision = _concrete_product_knowledge_decision(
        ctx,
        mismatch_type=mismatch_type,
    )
    _apply_suspend_scope(ctx, understanding, mismatch_type=mismatch_type)
    if product_knowledge_decision is not None:
        product_knowledge_decision.args = {
            **dict(product_knowledge_decision.args or {}),
            "turn_arbiter_enforced": True,
            "turn_arbiter_mismatch_type": mismatch_type,
            "block_order_flow": True,
        }
        enforced_decision = product_knowledge_decision
    else:
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
        "reply_goal=%s compose_mode=%s preserve_product_knowledge=%s "
        "suspend_stale=%s preview=%r",
        ctx.tenant_id,
        mismatch_type,
        proposed_owner,
        legacy_owner,
        legacy_action,
        enforced_decision.action,
        str((enforced_decision.args or {}).get("response_goal") or arbitration.owner_brief.reply_goal),
        str((enforced_decision.args or {}).get("compose_mode") or arbitration.owner_brief.compose_mode),
        str(product_knowledge_decision is not None).lower(),
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


_ORDER_SUPPORT_PRESERVE_PROPOSED_OWNERS = frozenset({
    OWNER_SUPPORT,
    OWNER_PERSONA_SOCIAL,
    OWNER_STAFF_ESCALATION,
})


def _should_preserve_authoritative_order_support(
    ctx: BrainContext,
    decision: Decision,
    arbitration: TurnArbitration,
) -> bool:
    """Keep an engine Order Support decision when a weaker contact signal disagrees.

    Once canonical Order Support ownership is proven and the engine already
    produced an Order Support-compatible decision (legacy owner tracking),
    Turn Arbiter must not reinterpret that same turn as staff/support/persona.
    """
    try:
        from ..commerce.order_support_ownership import (  # noqa: PLC0415
            has_authoritative_order_support_ownership,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[TURN_ARBITER_ENFORCE] order_support_ownership_probe_failed"
        )
        return False
    if not has_authoritative_order_support_ownership(
        getattr(ctx, "intent", None),
        state=getattr(ctx, "state", None),
    ):
        return False
    if legacy_owner_from_decision(decision) != OWNER_TRACKING:
        return False
    proposed = str(getattr(arbitration, "turn_owner", "") or "")
    return proposed in _ORDER_SUPPORT_PRESERVE_PROPOSED_OWNERS


__all__ = ["TurnEnforceResult", "maybe_enforce_turn_decision"]
