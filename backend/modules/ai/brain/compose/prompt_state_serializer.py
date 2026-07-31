"""
prompt_state_serializer.py
──────────────────────────
PR2C — commerce prompt payload slimming for MerchantBrain LLM compose.

Reduces system prompt size for routine commerce turns without changing
availability guards, product resolution, or card/media dispatch behavior.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from core.config import _bool_env

from ..intent_priority.types import (
    GOAL_PRICE_INQUIRY,
    GOAL_PRODUCT_AVAILABILITY,
)
from ..types import (
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    BrainReplyState,
)
from .prompt_payload_slim import (
    _KB_TRUNCATION_MARKER,
    _checkout_is_active,
    is_routine_social_turn,
    strip_state_dict_for_prompt,
)

_log = logging.getLogger("nahla.ai.commerce_prompt_slim")

_SLIM_FLAG = "NAHLA_COMMERCE_PROMPT_SLIM_ENABLED"
_MAX_CHARS_ENV = "NAHLA_COMMERCE_PROMPT_MAX_CHARS"
_KB_MAX_ENV = "NAHLA_COMMERCE_KB_MAX_CHARS"

_COMMERCE_SLIM_INTENTS: FrozenSet[str] = frozenset({
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_SOLUTION_SEEKING_COMMERCE,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    "product_availability",
    "product_reference",
})

_COMMERCE_SLIM_GOALS: FrozenSet[str] = frozenset({
    GOAL_PRODUCT_AVAILABILITY,
    "product_reference",
    GOAL_PRICE_INQUIRY,
})

_AI_SETTINGS_KEEP: FrozenSet[str] = frozenset({
    "reply_tone",
    "default_language",
    "reply_length",
    "assistant_name",
    "assistant_role",
    "store_display_name",
})

_CHECKOUT_FACT_KEYS: FrozenSet[str] = frozenset({
    "order_status",
    "awaiting_payment_receipt",
    "payment_receipt_received",
    "awaiting_variant_choice",
    "awaiting_option_confirmation",
    "payment_claim_unverified",
    "product_id",
    "variant_id",
    "quantity",
    "payment_method",
})

_PRODUCT_JSON_KEEP: FrozenSet[str] = frozenset({
    "id",
    "title",
    "name",
    "price",
    "currency",
    "available",
    "in_stock",
    "stock_status",
    "sku",
    "variant_id",
})

_COMPACT_RESOLVER_PROTOCOL = (
    "## بروتوكول الوسائط والمنتجات (مختصر)\n"
    "- طلب منتج → [PRODUCT:اسم من الكتالوج] في بداية الرد.\n"
    "- باركود/صورة/فيديو → [MEDIA_KEY:مفتاح من القائمة] في بداية الرد.\n"
    "- رقم موظف → [CALL:<رقم>|<اسم>] في نهاية الرد.\n"
    "- لا تكتبي https:// كنص — النظام يضيف الروابط تلقائياً.\n"
    "- سؤال سعر + selected_product فيه سعر رقمي موثوق → اذكري الرقم والعملة نصًا؛ "
    "[PRODUCT:…] اختياري للبطاقة ولا يُغني عن الإجابة النصية.\n"
    "- لا تخترعي أسعاراً — استخدمي فقط السعر من selected_product/merchant_context؛ "
    "غياب السعر = لا تذكري رقمًا.\n"
)

_NEED_ADVICE_SLIM_APPENDIX = (
    "### استشارة تجارية (مختصر)\n"
    "- أجيبي على الحاجة أو الصفة — لا تطلبي اسم SKU أولاً.\n"
    "- استخدمي selected_product وBrainStateJSON وFacts فقط.\n"
)

_COMMERCE_SLIM_RESIDUAL_RULES = (
    "## قواعد تشغيل Brain (commerce slim)\n"
    "- اتبعي stage وresponse_goal وselected_product.\n"
    "- ردّي باختصار (2–5 أسطر) مناسب لواتساب.\n"
    "- لا تخترعي حقائق — استخدمي BrainStateJSON وFacts فقط.\n"
    "- سؤال متابعة واحد عند نقص المعلومة.\n"
    "### أسلوب الرد التجاري (إلزامي — واتساب سعودي)\n"
    "- ردّي كموظف/موظفة متجر سعودي ودود — طبيعي وقريب، لا رسمي زائد.\n"
    "- استخدمي 1–2 إيموجي مناسبين عند الحاجة (🛒 ✨ 🍯 🚚 📍 ✅) — لا مبالغة.\n"
    "- لا تستخدمي إيموجي مرح في الشكاوى أو التصعيد.\n"
    "- تجنّبي العبارات الباردة: «هل ترغب بالمزيد من المعلومات؟»، "
    "«يمكنني مساعدتك في...»، «يرجى تحديد...».\n"
    "- اسألي سؤال متابعة بسيط ومفيد مثل: «وش الحجم اللي يناسبك؟» "
    "أو «تحب أرسل لك الخيارات؟».\n"
    "- لا تعدّي بتوفر أو سعر أو توصيل مؤكد إلا إذا كانت الحقيقة في "
    "BrainStateJSON أو Facts.\n"
    "- إيموجي ✈️ للطيفة التسويقية فقط (مثل «طيارة») — لا يعني شحنًا جويًا "
    "ولا وعدًا زمنيًا.\n"
    "- للاستعجال: ⚡ أو 🚀؛ للتوصيل: 🚚 أو 📍؛ لا تعد بـ«خلال دقائق» "
    "إلا إذا أكدها النظام.\n"
    "### عقد الرد (إلزامي)\n"
    "- اكتبي بالعربية فقط إذا كانت رسالة العميل عربية.\n"
    "- لا تكتبي Powered by Nahla ولا أي تذييل داخلي.\n"
    "- لا تكتبي أي جملة إنجليزية.\n"
    "- لا تقلي Let me verify ولا current availability.\n"
    "- لا تذكري أنك تتحقق إلا إذا كان لا يمكن الإجابة الآن.\n"
    "- في سؤال التوفر: أجيبي مباشرة أو اسألي عن الحجم/الوزن.\n"
)

_PAYMENT_ORDER_INTENTS: FrozenSet[str] = frozenset({
    "track_order",
    "pay_now",
    "start_order",
    "ask_payment_info",
})

_DISCOVERY_STAGES: FrozenSet[str] = frozenset({
    "discovery",
    "exploring",
    "deciding",
})

_IDLE_ORDER_STATUSES: FrozenSet[str] = frozenset({
    "none",
    "idle",
    "new",
    "",
})


def is_commerce_prompt_slim_enabled() -> bool:
    return _bool_env(_SLIM_FLAG, "false")


def _state_relevance_verdict_from_state(
    state: BrainReplyState,
) -> Optional["StateRelevanceVerdict"]:
    from ..state.state_relevance import StateRelevanceVerdict  # noqa: PLC0415

    raw = dict(
        (getattr(state, "known_facts", None) or {}).get("state_relevance_verdict") or {}
    )
    if not raw:
        return None
    workflows = raw.get("active_workflows") or []
    return StateRelevanceVerdict(
        payment_state_relevant=bool(raw.get("payment_state_relevant")),
        fulfillment_state_relevant=bool(raw.get("fulfillment_state_relevant")),
        product_replay_relevant=bool(raw.get("product_replay_relevant")),
        addon_recommendation_relevant=bool(raw.get("addon_recommendation_relevant")),
        stale_product_focus_relevant=bool(raw.get("stale_product_focus_relevant")),
        pending_candidates_relevant=bool(raw.get("pending_candidates_relevant")),
        safe_to_resume_state=bool(raw.get("safe_to_resume_state", True)),
        detected_topic_shift=bool(raw.get("detected_topic_shift")),
        relevance_confidence=float(raw.get("relevance_confidence") or 0.5),
        active_workflows=tuple(workflows),
        current_intent_hint=str(raw.get("current_intent_hint") or ""),
    )


def _is_commerce_info_slim_turn(state: BrainReplyState) -> bool:
    intent = str(getattr(state, "intent_name", "") or "").strip().lower()
    goal = str(getattr(state, "primary_customer_goal", "") or "").strip().lower()
    if intent in _PAYMENT_ORDER_INTENTS:
        return False
    return intent in _COMMERCE_SLIM_INTENTS or goal in _COMMERCE_SLIM_GOALS


def _collect_active_checkout_flags(checkout: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    for key in (
        "awaiting_payment_receipt",
        "payment_receipt_received",
        "awaiting_variant_choice",
        "awaiting_option_confirmation",
        "payment_claim_unverified",
    ):
        if checkout.get(key):
            flags.append(key)
    status = str(checkout.get("order_status") or "").strip().lower()
    if status and status not in _IDLE_ORDER_STATUSES:
        flags.append(f"order_status:{status}")
    return flags


def _has_payment_checkout_flags(active_flags: List[str]) -> bool:
    if any(
        flag in active_flags
        for flag in (
            "awaiting_payment_receipt",
            "payment_receipt_received",
            "payment_claim_unverified",
        )
    ):
        return True
    return any(flag.startswith("order_status:") for flag in active_flags)


def _stale_checkout_allows_commerce_slim(
    state: BrainReplyState,
    *,
    active_flags: List[str],
    verdict: Optional["StateRelevanceVerdict"],
) -> bool:
    if not _is_commerce_info_slim_turn(state):
        return False
    if not active_flags:
        return False

    if verdict is not None:
        if not verdict.detected_topic_shift:
            return False
        from ..state.state_relevance import should_block_workflow_resume  # noqa: PLC0415

        if _has_payment_checkout_flags(active_flags) and should_block_workflow_resume(
            "awaiting_payment_receipt",
            verdict,
        ):
            return True
        if (
            "awaiting_variant_choice" in active_flags
            or "awaiting_option_confirmation" in active_flags
        ) and should_block_workflow_resume("active_fulfillment", verdict):
            return True
        if "pending_candidates" in (verdict.active_workflows or ()) and should_block_workflow_resume(
            "pending_candidates",
            verdict,
        ):
            return True
        return False

    stage = str(getattr(state, "stage", "") or "").strip().lower()
    goal = str(getattr(state, "primary_customer_goal", "") or "").strip().lower()
    return (
        stage in _DISCOVERY_STAGES
        and goal == GOAL_PRODUCT_AVAILABILITY
        and _has_payment_checkout_flags(active_flags)
    )


def _evaluate_checkout_slim_blocker(
    state: BrainReplyState,
) -> Tuple[bool, Dict[str, Any]]:
    """Return (blocks_slim, telemetry) for checkout/payment stale-state gating."""
    checkout = dict(
        (getattr(state, "known_facts", None) or {}).get("checkout_preparation") or {}
    )
    stage = str(getattr(state, "stage", "") or "").strip().lower()
    active_flags = _collect_active_checkout_flags(checkout)
    verdict = _state_relevance_verdict_from_state(state)

    meta: Dict[str, Any] = {
        "checkout_blocked": False,
        "checkout_relevant": bool(active_flags),
        "state_topic_shift": bool(verdict.detected_topic_shift) if verdict else False,
        "active_checkout_flags": active_flags,
    }

    if stage in {"ordering", "checkout"}:
        meta["checkout_blocked"] = True
        meta["checkout_relevant"] = True
        return True, meta

    if not active_flags:
        meta["checkout_relevant"] = False
        return False, meta

    if (
        verdict is not None
        and verdict.detected_topic_shift
        and _is_commerce_info_slim_turn(state)
        and not _has_payment_checkout_flags(active_flags)
    ):
        meta["state_topic_shift"] = True
        meta["checkout_relevant"] = False
        meta["checkout_blocked"] = False
        return False, meta

    if _stale_checkout_allows_commerce_slim(
        state,
        active_flags=active_flags,
        verdict=verdict,
    ):
        meta["checkout_relevant"] = False
        meta["checkout_blocked"] = False
        return False, meta

    if verdict is not None:
        meta["checkout_relevant"] = bool(
            verdict.payment_state_relevant or verdict.fulfillment_state_relevant
        )
        if verdict.payment_state_relevant and _has_payment_checkout_flags(active_flags):
            meta["checkout_blocked"] = True
            return True, meta
        if verdict.fulfillment_state_relevant and any(
            flag in active_flags
            for flag in ("awaiting_variant_choice", "awaiting_option_confirmation")
        ):
            meta["checkout_blocked"] = True
            return True, meta

    meta["checkout_blocked"] = True
    meta["checkout_relevant"] = True
    return True, meta


def _checkout_blocks_commerce_prompt_slim(state: BrainReplyState) -> bool:
    blocked, _meta = _evaluate_checkout_slim_blocker(state)
    return blocked


def explain_commerce_prompt_slim_gate(
    state: BrainReplyState,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Return eligibility, reason code, and safe decision fields for audit."""
    mc = getattr(state, "merchant_context", None) or {}
    intent = str(getattr(state, "intent_name", "") or "").strip().lower()
    goal = str(getattr(state, "primary_customer_goal", "") or "").strip().lower()
    stage = str(getattr(state, "stage", "") or "").strip().lower()
    need_based = bool(getattr(state, "need_based_advice_mode", False))
    flag_enabled = is_commerce_prompt_slim_enabled()
    eligible_intent = intent in _COMMERCE_SLIM_INTENTS or goal in _COMMERCE_SLIM_GOALS
    eligible_stage = stage not in {"ordering", "checkout"}

    decision: Dict[str, Any] = {
        "tenant_id": mc.get("tenant_id") if isinstance(mc, dict) else None,
        "intent": intent or None,
        "stage": stage or None,
        "flag_enabled": flag_enabled,
        "need_based_advice_mode": need_based,
        "eligible_intent": eligible_intent,
        "eligible_stage": eligible_stage,
        "selected_path": "legacy",
        "commerce_slim_applied": False,
        "reason_if_false": "",
    }

    if not flag_enabled:
        decision["reason_if_false"] = "flag_disabled"
        return False, "flag_disabled", decision
    if is_routine_social_turn(state):
        decision["reason_if_false"] = "routine_social"
        return False, "routine_social", decision
    if bool(getattr(state, "platform_kb_mode", False)):
        decision["reason_if_false"] = "platform_kb_mode"
        return False, "platform_kb_mode", decision
    if bool(getattr(state, "contextual_clarify_mode", False)):
        decision["reason_if_false"] = "contextual_clarify_mode"
        return False, "contextual_clarify_mode", decision
    checkout_blocked, checkout_meta = _evaluate_checkout_slim_blocker(state)
    decision.update(checkout_meta)
    if checkout_blocked:
        decision["reason_if_false"] = "active_checkout"
        return False, "active_checkout", decision
    if not eligible_intent:
        decision["reason_if_false"] = "intent_or_goal_not_eligible"
        return False, "intent_or_goal_not_eligible", decision

    decision["selected_path"] = "commerce_slim"
    return True, "eligible", decision


def should_apply_commerce_prompt_slim(state: BrainReplyState) -> bool:
    eligible, _, _ = explain_commerce_prompt_slim_gate(state)
    return eligible


def commerce_prompt_max_chars() -> int:
    raw = os.getenv(_MAX_CHARS_ENV, "25000").strip()
    try:
        return max(5000, int(raw))
    except ValueError:
        return 25000


def commerce_kb_max_chars() -> int:
    raw = os.getenv(_KB_MAX_ENV, "3500").strip()
    try:
        return max(500, int(raw))
    except ValueError:
        return 5000


def slim_ai_settings_for_commerce_prompt(settings: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    slim = {k: settings[k] for k in _AI_SETTINGS_KEEP if k in settings}
    owner = str(settings.get("owner_instructions") or "").strip()
    if owner:
        slim["owner_instructions"] = owner[:500]
    return slim


def should_omit_kb_block_for_commerce_slim(state: BrainReplyState) -> bool:
    """Drop KB Facts block when structured product/availability context suffices."""
    if not should_apply_commerce_prompt_slim(state):
        return False
    goal = str(getattr(state, "primary_customer_goal", "") or "").strip().lower()
    if goal not in {GOAL_PRODUCT_AVAILABILITY, "product_reference", GOAL_PRICE_INQUIRY}:
        return False
    mc = getattr(state, "merchant_context", None) or {}
    structured_kb = ""
    if isinstance(mc, dict):
        structured_kb = str(mc.get("structured_facts_block") or "").strip()
    if goal in {GOAL_PRODUCT_AVAILABILITY, "product_reference"} and structured_kb:
        return True
    if not isinstance(getattr(state, "selected_product", None), dict):
        return False
    facts = dict(getattr(state, "known_facts", None) or {})
    return bool(facts.get("availability") or facts.get("product_focus"))


def cap_commerce_kb_block(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    limit = commerce_kb_max_chars()
    if len(body) <= limit:
        return body
    return body[:limit] + _KB_TRUNCATION_MARKER


def slim_resolver_overlay_for_commerce(resolver_overlay: str) -> str:
    overlay = (resolver_overlay or "").strip()
    if not overlay:
        return ""
    keys_block = ""
    marker = "أدوات الوسائط المتوفرة في هذا المتجر:"
    if marker in overlay:
        keys_block = overlay.split(marker, 1)[1].strip()
    parts = [_COMPACT_RESOLVER_PROTOCOL]
    if keys_block:
        parts.append(f"\n{marker}\n{keys_block[:2000]}")
    return "\n".join(parts)


def slim_libraries_for_commerce(
    libraries_text: str,
    *,
    include_coupons: bool,
) -> str:
    if not include_coupons:
        return ""
    text = (libraries_text or "").strip()
    if len(text) <= 1500:
        return text
    return text[:1500] + "\n[... libraries truncated for commerce slim ...]"


def _slim_product_row(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in _PRODUCT_JSON_KEEP if k in row}


def _slim_known_facts(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    checkout = raw.get("checkout_preparation")
    if isinstance(checkout, dict):
        slim_checkout = {
            k: checkout[k]
            for k in _CHECKOUT_FACT_KEYS
            if k in checkout and checkout[k] not in (None, "", False)
        }
        if slim_checkout:
            out["checkout_preparation"] = slim_checkout
    for key in ("availability", "product_focus", "payment_flags", "fulfillment_flags"):
        if key in raw and raw[key]:
            out[key] = raw[key]
    tc_facts = raw.get("trusted_coupon_offer_facts")
    if isinstance(tc_facts, dict) and tc_facts:
        out["trusted_coupon_offer_facts"] = tc_facts
    tc_projection = raw.get("trusted_context_projection")
    if isinstance(tc_projection, dict) and tc_projection:
        out["trusted_context_projection"] = tc_projection
    return out


def serialize_commerce_brain_state(
    state_dict: Dict[str, Any],
    state: BrainReplyState,
    *,
    kb_in_prompt_block: bool,
) -> Dict[str, Any]:
    """Aggressive BrainStateJSON for commerce slim turns."""
    base = strip_state_dict_for_prompt(
        state_dict,
        state,
        kb_in_prompt_block=kb_in_prompt_block,
        force_commerce_lite=True,
    )
    out = dict(base)

    for key in (
        "store_knowledge",
        "conversation_summary",
        "tenant_overlay",
        "coupon_policy",
        "policy_reason",
        "explicit_pending_action",
        "last_recommended_products",
    ):
        out.pop(key, None)

    turns = out.get("recent_turns")
    if isinstance(turns, list):
        out["recent_turns"] = list(turns)[-2:]

    memory = out.get("customer_memory")
    if isinstance(memory, dict):
        out["customer_memory"] = {
            k: memory[k]
            for k in ("segment", "is_returning")
            if k in memory
        }

    facts = _slim_known_facts(out.get("known_facts"))
    if facts:
        out["known_facts"] = facts
    else:
        out.pop("known_facts", None)

    mc = dict(out.get("merchant_context") or {})
    if mc:
        slim_mc: Dict[str, Any] = {}
        if mc.get("tenant_id") is not None:
            slim_mc["tenant_id"] = mc["tenant_id"]
        products = mc.get("products")
        if isinstance(products, list):
            slim_mc["products"] = [
                _slim_product_row(p) for p in products[:3] if _slim_product_row(p)
            ]
        profile = mc.get("brain_profile")
        if isinstance(profile, dict):
            slim_mc["brain_profile"] = {
                k: profile[k]
                for k in ("autopilot_enabled", "orderable", "tenant_id")
                if k in profile
            }
        for flag_key in ("payment_enabled", "shipping_enabled", "cod_enabled"):
            if flag_key in mc:
                slim_mc[flag_key] = mc[flag_key]
        out["merchant_context"] = slim_mc

    return out


def measure_commerce_prompt_contributors(
    state: BrainReplyState,
    *,
    kb_block: str = "",
    structured_behavior_block: str = "",
) -> Dict[str, int]:
    """Top contributor sizes for audit — no customer message content."""
    mc = dict(getattr(state, "merchant_context", None) or {})
    ai = dict(mc.get("ai_settings") or {})
    manual_kb = str(
        ai.get("manual_knowledge_base")
        or ai.get("manual_knowledge_base_v2")
        or ""
    )
    structured_facts = str(mc.get("structured_facts_block") or "")
    products = mc.get("products") or []
    return {
        "ai_settings_chars": len(json.dumps(ai, ensure_ascii=False)),
        "manual_kb_chars": len(manual_kb),
        "structured_facts_block_chars": len(structured_facts),
        "structured_behavior_block_chars": len(structured_behavior_block or ""),
        "product_context_chars": len(
            json.dumps(getattr(state, "selected_product", None) or {}, ensure_ascii=False)
        ),
        "catalog_products_chars": len(json.dumps(products, ensure_ascii=False)),
        "kb_block_chars": len(kb_block or ""),
        "resolver_overlay_chars": len(str(mc.get("resolver_overlay") or "")),
        "duplicated_structured_facts_in_json": int(
            bool(kb_block)
            and "structured_facts_block" in mc
            and structured_facts[:80] in kb_block[: max(len(structured_facts), 80)]
        ),
    }


def emit_commerce_prompt_contributors_audit(
    *,
    state: BrainReplyState,
    contributors: Dict[str, int],
    slim_applied: bool,
    total_prompt_chars: Optional[int] = None,
) -> None:
    try:
        mc = state.merchant_context or {}
        payload = {
            "event": "commerce_prompt_contributors",
            "tenant_id": mc.get("tenant_id"),
            "intent": getattr(state, "intent_name", None),
            "slim_applied": slim_applied,
            "total_prompt_chars": total_prompt_chars,
            **contributors,
        }
        top = sorted(
            ((k, v) for k, v in contributors.items() if k.endswith("_chars")),
            key=lambda item: item[1],
            reverse=True,
        )[:6]
        payload["top_contributors"] = [k for k, _ in top]
        _log.info(
            "[COMMERCE_PROMPT_CONTRIBUTORS] %s",
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[COMMERCE_PROMPT_CONTRIBUTORS_ERROR] err=%s",
            type(exc).__name__,
        )


def emit_commerce_prompt_slim_error(*, err: str, intent: Optional[str] = None) -> None:
    _log.warning(
        "[COMMERCE_PROMPT_SLIM_ERROR] %s",
        json.dumps({"intent": intent, "err": err}, ensure_ascii=False),
    )


def emit_commerce_prompt_slim_decision(decision: Dict[str, Any]) -> None:
    try:
        _log.info(
            "[COMMERCE_PROMPT_SLIM_DECISION] %s",
            json.dumps(decision, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[COMMERCE_PROMPT_SLIM_ERROR] %s",
            json.dumps({"err": type(exc).__name__}, ensure_ascii=False),
        )


def emit_commerce_prompt_slim_applied(
    *,
    state: BrainReplyState,
    before_chars: int,
    after_chars: int,
    removed_ai_settings: bool,
    system_chars_before: int,
    system_chars_after: int,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
) -> None:
    try:
        mc = state.merchant_context or {}
        payload = {
            "tenant_id": mc.get("tenant_id"),
            "conversation_id": conversation_id if conversation_id is not None else mc.get("conversation_id"),
            "turn_id": turn_id if turn_id is not None else mc.get("turn_id"),
            "intent": getattr(state, "intent_name", None),
            "before_chars": before_chars,
            "after_chars": after_chars,
            "removed_ai_settings": removed_ai_settings,
            "system_chars_before": system_chars_before,
            "system_chars_after": system_chars_after,
        }
        _log.info(
            "[COMMERCE_PROMPT_SLIM_APPLIED] %s",
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001 — audit must never break replies
        _log.warning(
            "[COMMERCE_PROMPT_SLIM_ERROR] %s",
            json.dumps({"err": type(exc).__name__}, ensure_ascii=False),
        )


@dataclass(frozen=True)
class CommercePromptSlimLayers:
    settings_for_overlay: Dict[str, Any]
    kb_block: str
    libraries_text: str
    resolver_overlay: str
    structured_behavior_block: str
    state_dict: Dict[str, Any]
    need_advice_appendix: str


def apply_commerce_prompt_slim_layers(
    *,
    state: BrainReplyState,
    settings_for_overlay: Dict[str, Any],
    kb_block: str,
    libraries_text: str,
    resolver_overlay: str,
    structured_behavior_block: str,
    state_dict: Dict[str, Any],
    kb_in_prompt_block: bool,
    need_advice_mode: bool,
) -> CommercePromptSlimLayers:
    slim_settings = slim_ai_settings_for_commerce_prompt(settings_for_overlay)
    slim_kb = cap_commerce_kb_block(kb_block)
    include_coupons = _checkout_is_active(state) or str(
        getattr(state, "intent_name", "") or ""
    ).strip().lower() in {INTENT_ASK_PRICE, "ask_payment_info"}
    slim_libraries = slim_libraries_for_commerce(
        libraries_text,
        include_coupons=include_coupons,
    )
    slim_resolver = slim_resolver_overlay_for_commerce(resolver_overlay)
    slim_behavior = (structured_behavior_block or "")[:1500]
    slim_state = serialize_commerce_brain_state(
        state_dict,
        state,
        kb_in_prompt_block=kb_in_prompt_block,
    )
    need_appendix = _NEED_ADVICE_SLIM_APPENDIX if need_advice_mode else ""
    return CommercePromptSlimLayers(
        settings_for_overlay=slim_settings,
        kb_block=slim_kb,
        libraries_text=slim_libraries,
        resolver_overlay=slim_resolver,
        structured_behavior_block=slim_behavior,
        state_dict=slim_state,
        need_advice_appendix=need_appendix,
    )
