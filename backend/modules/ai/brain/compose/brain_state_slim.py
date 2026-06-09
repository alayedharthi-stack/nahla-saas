"""
brain/compose/brain_state_slim.py
───────────────────────────────────
Phase 2b — slim BrainStateJSON for general / non-commerce turns only.

Phase B0 — unified ``[BRAIN_STATE_SLIM]`` v2 telemetry for persona + Phase 2b
paths, including shadow Persona JSON Contract metrics (measurement only).
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
_SLIM_LOG_SCHEMA_VERSION = 2

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


def _json_char_count(state_dict: Dict[str, Any]) -> int:
    return len(json.dumps(state_dict, ensure_ascii=False, indent=2))


def top_json_field_contributors(
    state_dict: Dict[str, Any],
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Rank top-level JSON fields by serialized char size (for B0 reports)."""
    ranked: List[Tuple[str, int]] = []
    for key, value in state_dict.items():
        try:
            size = len(json.dumps({key: value}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            size = 0
        ranked.append((key, size))
    ranked.sort(key=lambda item: item[1], reverse=True)
    out: List[Dict[str, Any]] = []
    for key, size in ranked[:limit]:
        entry: Dict[str, Any] = {"field": key, "chars": size}
        if key == "merchant_context" and isinstance(state_dict.get(key), dict):
            mc = state_dict[key]
            mc_ranked: List[Tuple[str, int]] = []
            for mk, mv in mc.items():
                try:
                    mc_size = len(json.dumps({mk: mv}, ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    mc_size = 0
                mc_ranked.append((mk, mc_size))
            mc_ranked.sort(key=lambda item: item[1], reverse=True)
            entry["merchant_context_top"] = [
                {"field": mk, "chars": ms} for mk, ms in mc_ranked[:3]
            ]
        out.append(entry)
    return out


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


def emit_brain_state_slim_v2(
    *,
    state: BrainReplyState,
    before_json_chars: int,
    actual_json_chars: int,
    slim_profile: str,
    was_slimmed: bool,
    slim_reason: str,
    removed_top_fields: List[str],
    contract_eligible: bool,
    contract_json_chars: int | None = None,
    contract_omitted_fields: List[str] | None = None,
    top_remaining_contributors: List[Dict[str, Any]] | None = None,
) -> None:
    """Emit unified B0 telemetry — observability only, no prompt side effects."""
    try:
        mc = getattr(state, "merchant_context", None) or {}
        tenant_id = mc.get("tenant_id") if isinstance(mc, dict) else None

        delta_json_chars: int | None = None
        if contract_eligible and contract_json_chars is not None:
            delta_json_chars = actual_json_chars - contract_json_chars
        elif was_slimmed:
            delta_json_chars = before_json_chars - actual_json_chars

        payload: Dict[str, Any] = {
            "event": "brain_state_slim",
            "schema_version": _SLIM_LOG_SCHEMA_VERSION,
            "slim_profile": slim_profile,
            "tenant_id": tenant_id,
            "intent": getattr(state, "intent_name", "") or None,
            "stage": getattr(state, "stage", "") or None,
            "persona_expression_mode": bool(
                getattr(state, "persona_expression_mode", False)
            ),
            "persona_topic": getattr(state, "persona_topic", "") or None,
            "persona_kind": getattr(state, "persona_kind", "") or None,
            "before_json_chars": before_json_chars,
            "actual_json_chars": actual_json_chars,
            "after_json_chars": actual_json_chars,
            "contract_eligible": contract_eligible,
            "contract_json_chars": contract_json_chars,
            "delta_json_chars": delta_json_chars,
            "was_slimmed": was_slimmed,
            "slim_reason": slim_reason,
            "removed_top_fields": removed_top_fields,
            "top_remaining_contributors": top_remaining_contributors or [],
            "prompt_unchanged": True,
        }
        if contract_omitted_fields is not None:
            payload["contract_omitted_fields"] = contract_omitted_fields
            payload["contract_omitted_fields_count"] = len(contract_omitted_fields)

        # Back-compat aliases for existing dashboards / greps.
        payload["old_json_chars"] = before_json_chars
        payload["new_json_chars"] = actual_json_chars
        payload["delta_chars"] = delta_json_chars

        _log.info("[BRAIN_STATE_SLIM] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def prepare_brain_state_dict_with_telemetry(
    state: BrainReplyState,
    state_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply the existing slim path (unchanged behavior) and emit v2 telemetry.

    Persona JSON Contract output is computed **only** for shadow metrics when
    ``contract_eligible``; the returned dict is always the current production slim.
    """
    from ..persona_expression import slim_brain_state_dict_for_persona  # noqa: PLC0415
    from .persona_json_contract import (  # noqa: PLC0415
        apply_persona_json_contract_shadow,
        is_persona_contract_eligible,
    )

    raw_dict = dict(state_dict)
    before_chars = _json_char_count(raw_dict)
    persona_mode = bool(getattr(state, "persona_expression_mode", False))

    contract_eligible = is_persona_contract_eligible(state)
    removed: List[str] = []
    if persona_mode:
        actual_dict = slim_brain_state_dict_for_persona(
            raw_dict,
            persona_topic=str(getattr(state, "persona_topic", "") or ""),
        )
        if contract_eligible:
            slim_profile = "persona_contract_shadow"
            slim_reason = "persona_contract_shadow_metrics"
        else:
            slim_profile = "persona_partial"
            slim_reason = "persona_partial_slim"
        for key in raw_dict:
            if key not in actual_dict:
                removed.append(key)
        mc_raw = raw_dict.get("merchant_context")
        mc_act = actual_dict.get("merchant_context")
        if isinstance(mc_raw, dict) and isinstance(mc_act, dict):
            mc_removed = [k for k in mc_raw if k not in mc_act]
            if mc_removed:
                removed.append("merchant_context." + ",".join(sorted(mc_removed)))
    else:
        eligible, reason = should_slim_general_brain_state(state)
        if is_slim_general_brain_state_enabled() and eligible:
            actual_dict, removed = slim_brain_state_dict_for_general(raw_dict)
            slim_profile = "phase2b_general"
            slim_reason = reason
        else:
            actual_dict = raw_dict
            slim_profile = "none"
            slim_reason = reason if not eligible else "flag_disabled"

    actual_chars = _json_char_count(actual_dict)
    was_slimmed = before_chars != actual_chars
    contract_chars: int | None = None
    contract_omitted: List[str] | None = None

    if contract_eligible:
        contract_dict, contract_omitted = apply_persona_json_contract_shadow(
            raw_dict,
            state=state,
        )
        contract_chars = _json_char_count(contract_dict)

    emit_brain_state_slim_v2(
        state=state,
        before_json_chars=before_chars,
        actual_json_chars=actual_chars,
        slim_profile=slim_profile,
        was_slimmed=was_slimmed,
        slim_reason=slim_reason,
        removed_top_fields=removed,
        contract_eligible=contract_eligible,
        contract_json_chars=contract_chars,
        contract_omitted_fields=contract_omitted,
        top_remaining_contributors=top_json_field_contributors(actual_dict),
    )
    return actual_dict


def maybe_slim_brain_state_dict(
    state: BrainReplyState,
    state_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Legacy entry — delegates to ``prepare_brain_state_dict_with_telemetry``.

    Kept for callers outside ``prompt_builder``; behavior unchanged except
    that v2 telemetry is always emitted.
    """
    return prepare_brain_state_dict_with_telemetry(state, state_dict)


def _emit_slim_log(
    *,
    state: BrainReplyState,
    was_slimmed: bool,
    slim_reason: str,
    old_json_chars: int,
    new_json_chars: int,
    removed_top_fields: List[str],
) -> None:
    """Deprecated v1 emitter — retained for tests importing private helper."""
    from .persona_json_contract import is_persona_contract_eligible  # noqa: PLC0415

    emit_brain_state_slim_v2(
        state=state,
        before_json_chars=old_json_chars,
        actual_json_chars=new_json_chars,
        slim_profile="phase2b_general" if was_slimmed else "none",
        was_slimmed=was_slimmed,
        slim_reason=slim_reason,
        removed_top_fields=removed_top_fields,
        contract_eligible=is_persona_contract_eligible(state),
    )


def is_persona_contract_eligible(state: BrainReplyState) -> bool:
    from .persona_json_contract import is_persona_contract_eligible as _fn  # noqa: PLC0415

    return _fn(state)


__all__ = [
    "emit_brain_state_slim_v2",
    "is_persona_contract_eligible",
    "is_slim_general_brain_state_enabled",
    "maybe_slim_brain_state_dict",
    "prepare_brain_state_dict_with_telemetry",
    "should_slim_general_brain_state",
    "slim_brain_state_dict_for_general",
    "top_json_field_contributors",
]
