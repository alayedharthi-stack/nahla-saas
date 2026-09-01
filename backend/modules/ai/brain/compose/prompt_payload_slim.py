"""
brain/compose/prompt_payload_slim.py
────────────────────────────────────
Prompt-size guards for MerchantBrain LLM compose.

Strips duplicate or oversized knowledge/catalog from the assembled prompt
payload without changing DB-stored merchant knowledge or Persona behavior.
"""
from __future__ import annotations

import json
import os

from core.config import _bool_env

from ..types import (
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_PAY_NOW,
    INTENT_PICK_LIST_ITEM,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainReplyState,
)

ROUTINE_SOCIAL_INTENTS: FrozenSet[str] = frozenset({
    "greeting",
    "social",
    "thanks",
    "farewell",
    "gratitude",
    "who_are_you",
    "persona_interaction",
    "hesitation",
})

_COMMERCE_PROMPT_INTENTS: FrozenSet[str] = frozenset({
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
})

_KB_HEAVY_AI_SETTINGS_KEYS: FrozenSet[str] = frozenset({
    "manual_knowledge_base",
    "manual_knowledge_base_v2",
})

_ROUTINE_TOP_OMIT: FrozenSet[str] = frozenset({
    "known_facts",
    "store_knowledge",
    "selected_product",
    "last_recommended_products",
    "coupon_policy",
    "recommended_next_step",
    "policy_reason",
    "explicit_pending_action",
    "customer_memory",
    "conversation_summary",
    "recent_turns",
})

_COMMERCE_LITE_TOP_OMIT: FrozenSet[str] = frozenset({
    "store_knowledge",
    "recent_turns",
    "conversation_summary",
    "tenant_overlay",
})

_MC_ROUTINE_KEEP: FrozenSet[str] = frozenset({"tenant_id"})

_MC_COMMERCE_LITE_OMIT: FrozenSet[str] = frozenset({
    "structured_facts_block",
    "structured_behavior_block",
    "ai_settings",
    "conversation",
    "faq_approved",
    "resolver_overlay",
    "retrieval_rules",
})

_KB_TRUNCATION_MARKER = "\n\n[... knowledge truncated for prompt size ...]"


def max_kb_prompt_chars() -> int:
    raw = os.getenv("NAHLA_MAX_KB_PROMPT_CHARS", "12000").strip()
    try:
        return max(1000, int(raw))
    except ValueError:
        return 12000


def cap_kb_for_prompt(text: str) -> str:
    """Cap KB block chars for LLM prompt only."""
    body = (text or "").strip()
    if not body:
        return ""
    limit = max_kb_prompt_chars()
    if len(body) <= limit:
        return body
    return body[:limit] + _KB_TRUNCATION_MARKER


def _authoritative_fact_contract_present(state: BrainReplyState) -> bool:
    """True when compose already holds the per-turn FactAnswer contract."""
    facts = getattr(state, "known_facts", None) or {}
    if not isinstance(facts, dict):
        return False
    contract = facts.get("answer_contract")
    if isinstance(contract, dict) and str(contract.get("fact_kind") or "").strip():
        return True
    cap = facts.get("merchant_capability_answer")
    if isinstance(cap, dict) and str(cap.get("question_kind") or "").strip() in {
        "shipping_companies",
        "payment_methods",
        "cash_on_delivery",
    }:
        return True
    coupon_facts = facts.get("customer_request_coupon_facts")
    if isinstance(coupon_facts, dict) and coupon_facts:
        return True
    return False


def is_routine_social_turn(state: BrainReplyState) -> bool:
    # Coarse SOCIAL/greeting must not erase a more specific fact owner.
    if _authoritative_fact_contract_present(state):
        return False
    if bool(getattr(state, "persona_expression_mode", False)):
        return True
    if bool(getattr(state, "non_commerce_block_mode", False)):
        return True
    mc = getattr(state, "merchant_context", None) or {}
    if isinstance(mc, dict) and mc.get("pre_commerce_social"):
        return True
    intent = str(getattr(state, "intent_name", "") or "").strip().lower()
    return intent in ROUTINE_SOCIAL_INTENTS


def resolve_kb_block_for_prompt(
    state: BrainReplyState,
    *,
    structured_kb: str,
    overlay_facts: str,
) -> str:
    """KB Facts block for the prompt — empty on routine social turns."""
    if is_routine_social_turn(state):
        return ""
    if bool(getattr(state, "persona_expression_mode", False)):
        return ""
    structured = (structured_kb or "").strip()
    if structured:
        return cap_kb_for_prompt(structured)
    mc = getattr(state, "merchant_context", None) or {}
    if isinstance(mc, dict) and mc.get("structured_overlay_held_empty"):
        return ""
    facts = (overlay_facts or "").strip()
    if facts:
        return cap_kb_for_prompt(facts)
    return ""


def _checkout_is_active(state: BrainReplyState) -> bool:
    checkout = dict(
        (getattr(state, "known_facts", None) or {}).get("checkout_preparation") or {}
    )
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


def _is_commerce_prompt_slim_flag_on() -> bool:
    return _bool_env("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "false")


def should_apply_commerce_lite(state: BrainReplyState) -> bool:
    if is_routine_social_turn(state):
        return False
    if bool(getattr(state, "platform_kb_mode", False)):
        return False
    if bool(getattr(state, "contextual_clarify_mode", False)):
        return False
    need_based = bool(getattr(state, "need_based_advice_mode", False))
    if need_based and not _is_commerce_prompt_slim_flag_on():
        return False
    if _checkout_is_active(state):
        return False
    intent = str(getattr(state, "intent_name", "") or "").strip().lower()
    return intent in _COMMERCE_PROMPT_INTENTS


def _slim_ai_settings_for_json(settings: Any) -> Dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    return {
        k: v
        for k, v in settings.items()
        if k not in _KB_HEAVY_AI_SETTINGS_KEYS
    }


def _slim_merchant_context_for_json(
    mc: Dict[str, Any],
    *,
    routine_social: bool,
    commerce_lite: bool,
    kb_in_prompt_block: bool,
) -> Dict[str, Any]:
    if routine_social:
        tid = mc.get("tenant_id")
        return {"tenant_id": tid} if tid is not None else {}

    out = dict(mc)
    if kb_in_prompt_block:
        out.pop("structured_facts_block", None)

    ai = out.get("ai_settings")
    if isinstance(ai, dict):
        out["ai_settings"] = _slim_ai_settings_for_json(ai)

    if commerce_lite:
        for key in _MC_COMMERCE_LITE_OMIT:
            out.pop(key, None)
        products = out.get("products")
        if isinstance(products, list):
            out["products"] = list(products)[:5]
        conversation = out.get("conversation")
        if isinstance(conversation, dict):
            slim_conv = dict(conversation)
            slim_conv.pop("recent_messages", None)
            out["conversation"] = slim_conv

    return out


def strip_state_dict_for_prompt(
    state_dict: Dict[str, Any],
    state: BrainReplyState,
    *,
    kb_in_prompt_block: bool,
    force_commerce_lite: bool = False,
) -> Dict[str, Any]:
    """
    Remove duplicate/heavy fields from BrainStateJSON before serialization.

    Does not mutate the caller's dict.
    """
    out = dict(state_dict)
    routine = is_routine_social_turn(state)
    commerce_lite = force_commerce_lite or should_apply_commerce_lite(state)

    if routine:
        identity_slice: Dict[str, Any] = {}
        try:
            from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
                merchant_identity_evidence_slice,
            )

            identity_slice = merchant_identity_evidence_slice(
                getattr(state, "known_facts", None) or out.get("known_facts") or {},
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — identity slice must not block slim
            identity_slice = {}
        for key in _ROUTINE_TOP_OMIT:
            out.pop(key, None)
        if identity_slice:
            out["known_facts"] = identity_slice
    elif commerce_lite:
        for key in _COMMERCE_LITE_TOP_OMIT:
            out.pop(key, None)
        memory = out.get("customer_memory")
        if isinstance(memory, dict):
            out["customer_memory"] = {
                k: memory[k]
                for k in ("segment", "is_returning", "phone")
                if k in memory
            }

    mc = out.get("merchant_context")
    if isinstance(mc, dict):
        out["merchant_context"] = _slim_merchant_context_for_json(
            mc,
            routine_social=routine,
            commerce_lite=commerce_lite,
            kb_in_prompt_block=kb_in_prompt_block,
        )

    return out


def measure_prompt_layer_chars(
    state: BrainReplyState,
    *,
    kb_block: str,
) -> Dict[str, int]:
    """Sizes for audit/telemetry — mirrors prompt assembly, no message text."""
    routine = is_routine_social_turn(state)
    mc = dict(getattr(state, "merchant_context", None) or {})
    kb_chars = len(kb_block or "")
    if routine:
        catalog_chars = 0
        tools_chars = 0
    else:
        catalog_chars = len(json.dumps(mc.get("products") or [], ensure_ascii=False))
        tools_chars = len(str(mc.get("resolver_overlay") or ""))
    product_context_chars = len(
        json.dumps(
            getattr(state, "selected_product", None) or {},
            ensure_ascii=False,
        )
    )
    return {
        "kb_chars": kb_chars,
        "catalog_chars": catalog_chars,
        "product_context_chars": product_context_chars,
        "tools_chars": tools_chars,
    }
