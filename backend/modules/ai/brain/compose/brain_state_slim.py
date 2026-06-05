"""
brain/compose/brain_state_slim.py
───────────────────────────────────
Phase 2b — slim BrainStateJSON for general / non-commerce turns only.

Removes operational noise from the JSON payload (not from upstream sources).
Platform-wide; no tenant-specific logic.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Tuple

from ..state.stages import (
    STAGE_CHECKOUT,
    STAGE_COMPLETE,
    STAGE_ORDERING,
    STAGE_SUPPORT,
)
from ..types import (
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_PAY_NOW,
    INTENT_PICK_LIST_ITEM,
    INTENT_PLATFORM_INQUIRY,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainReplyState,
)

_log = logging.getLogger("nahla.ai.brain_state_slim")

_FLAG = "NAHLA_SLIM_GENERAL_BRAIN_STATE_ENABLED"

_COMMERCE_INTENTS = frozenset({
    INTENT_ASK_PRODUCT,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_TRACK_ORDER,
    INTENT_PICK_LIST_ITEM,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_PLATFORM_INQUIRY,
})

_ORDER_STAGES = frozenset({
    STAGE_ORDERING,
    STAGE_CHECKOUT,
    STAGE_COMPLETE,
    STAGE_SUPPORT,
})

# Top-level keys dropped in general slim mode.
_SLIM_OMIT_TOP_KEYS = frozenset({
    "known_facts",
    "store_knowledge",
    "selected_product",
    "last_recommended_products",
    "coupon_policy",
    "recommended_next_step",
    "policy_reason",
    "explicit_pending_action",
    "platform_kb_excerpt",
    "platform_kb_mode",
    "platform_topic",
    "need_based_advice_mode",
    "need_category",
    "contextual_clarify_mode",
    "ambiguity_class",
    "clarification_evidence",
    "non_commerce_block_mode",
    "price_sensitivity",
    "response_goal",
    "tenant_overlay",
})

# merchant_context keys dropped (operational); tenant_id kept for traceability only.
_SLIM_OMIT_MERCHANT_CONTEXT_KEYS = frozenset({
    "ai_settings",
    "structured_facts_block",
    "structured_behavior_block",
    "products",
    "policies",
    "policy_presence",
    "faq_approved",
    "resolver_overlay",
    "brain_profile",
    "retrieval_rules",
    "tenant_profile",
    "customer",
    "conversation",
})


def is_slim_general_brain_state_enabled() -> bool:
    return os.getenv(_FLAG, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def should_slim_general_brain_state(state: BrainReplyState) -> Tuple[bool, str]:
    """
    Conservative gate — slim only when the turn is clearly non-operational.
    """
    if bool(getattr(state, "persona_expression_mode", False)):
        return False, "persona_expression_mode"

    if bool(getattr(state, "platform_kb_mode", False)):
        return False, "platform_kb_mode"

    if bool(getattr(state, "need_based_advice_mode", False)):
        return False, "need_based_advice_mode"

    if bool(getattr(state, "contextual_clarify_mode", False)):
        return False, "contextual_clarify_mode"

    intent = str(getattr(state, "intent_name", "") or "").strip().lower()
    if intent in _COMMERCE_INTENTS:
        return False, f"commerce_intent:{intent}"

    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage in _ORDER_STAGES:
        return False, f"order_stage:{stage}"

    if getattr(state, "selected_product", None):
        return False, "selected_product_focus"

    pending = str(getattr(state, "explicit_pending_action", "") or "").strip()
    if pending:
        return False, "explicit_pending_action"

    checkout = dict((getattr(state, "known_facts", None) or {}).get("checkout_preparation") or {})
    if _checkout_is_active(checkout):
        return False, "active_order_flow"

    if intent == "general":
        return True, "intent_general"

    # Non-commerce social / phatic intents (not in commerce blocklist).
    _NON_COMMERCE_OK = frozenset({
        "social",
        "greeting",
        "who_are_you",
        "persona_interaction",
        "hesitation",
        "talk_to_human",
        "employee_not_responding",
    })
    if intent in _NON_COMMERCE_OK:
        return True, f"non_commerce_intent:{intent}"

    return False, f"intent_not_eligible:{intent or 'unknown'}"


def _checkout_is_active(checkout: Dict[str, Any]) -> bool:
    status = str(checkout.get("order_status") or "").strip().lower()
    if status and status not in {"none", "idle", "new", ""}:
        return True
    for flag in (
        "awaiting_payment_receipt",
        "payment_receipt_received",
        "awaiting_variant_choice",
        "awaiting_option_confirmation",
        "payment_claim_unverified",
    ):
        if checkout.get(flag):
            return True
    if str(checkout.get("product_id") or "").strip():
        return True
    return False


def slim_brain_state_dict_for_general(
    state_dict: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Return slimmed dict and list of removed top-level field names."""
    out = dict(state_dict)
    removed: List[str] = []

    for key in _SLIM_OMIT_TOP_KEYS:
        if key in out:
            out.pop(key, None)
            removed.append(key)

    mc = out.get("merchant_context")
    if isinstance(mc, dict) and mc:
        slim_mc: Dict[str, Any] = {}
        tid = mc.get("tenant_id")
        if tid is not None:
            slim_mc["tenant_id"] = tid
        removed_mc = [k for k in mc if k in _SLIM_OMIT_MERCHANT_CONTEXT_KEYS]
        if removed_mc:
            removed.append("merchant_context." + ",".join(sorted(removed_mc)))
        out["merchant_context"] = slim_mc

    return out, removed


def maybe_slim_brain_state_dict(
    state: BrainReplyState,
    state_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply general slim when flag + gate allow. Emits ``[BRAIN_STATE_SLIM]`` log.
    """
    old_json = json.dumps(state_dict, ensure_ascii=False, indent=2)
    old_chars = len(old_json)

    if not is_slim_general_brain_state_enabled():
        return state_dict

    eligible, reason = should_slim_general_brain_state(state)
    if not eligible:
        _emit_slim_log(
            state=state,
            was_slimmed=False,
            slim_reason=reason,
            old_json_chars=old_chars,
            new_json_chars=old_chars,
            removed_top_fields=[],
        )
        return state_dict

    slimmed, removed = slim_brain_state_dict_for_general(state_dict)
    new_chars = len(json.dumps(slimmed, ensure_ascii=False, indent=2))
    _emit_slim_log(
        state=state,
        was_slimmed=True,
        slim_reason=reason,
        old_json_chars=old_chars,
        new_json_chars=new_chars,
        removed_top_fields=removed,
    )
    return slimmed


def _emit_slim_log(
    *,
    state: BrainReplyState,
    was_slimmed: bool,
    slim_reason: str,
    old_json_chars: int,
    new_json_chars: int,
    removed_top_fields: List[str],
) -> None:
    try:
        mc = getattr(state, "merchant_context", None) or {}
        tenant_id = mc.get("tenant_id") if isinstance(mc, dict) else None
        payload = {
            "event": "brain_state_slim",
            "tenant_id": tenant_id,
            "intent": getattr(state, "intent_name", "") or None,
            "stage": getattr(state, "stage", "") or None,
            "old_json_chars": old_json_chars,
            "new_json_chars": new_json_chars,
            "removed_top_fields": removed_top_fields,
            "slim_reason": slim_reason,
            "was_slimmed": was_slimmed,
        }
        _log.info("[BRAIN_STATE_SLIM] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "is_slim_general_brain_state_enabled",
    "maybe_slim_brain_state_dict",
    "should_slim_general_brain_state",
    "slim_brain_state_dict_for_general",
]
