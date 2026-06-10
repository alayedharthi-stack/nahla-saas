"""
Persona expression helpers — Phase 3A/3B compose profile (routing unchanged).

Subtracts commerce-oriented prompt layers on ``persona_identity`` /
``persona_social`` turns. Behavioral guidance only — no canned Arabic.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

PERSONA_TOPIC_IDENTITY = "persona_identity"
PERSONA_TOPIC_SOCIAL = "persona_social"
PERSONA_TOPIC_SOCIAL_PERSONA_ACK = "social_persona_ack"
PERSONA_TOPIC_NON_SALES_AMBIGUOUS = "non_sales_ambiguous"

PERSONA_KIND_GREETING = "greeting"

_ESTABLISHED_GREET_PERSONA_FLAG = "ESTABLISHED_GREET_PERSONA_COMPOSE_ENABLED"

PERSONA_TOPICS = frozenset({
    PERSONA_TOPIC_IDENTITY,
    PERSONA_TOPIC_SOCIAL,
    PERSONA_TOPIC_SOCIAL_PERSONA_ACK,
    PERSONA_TOPIC_NON_SALES_AMBIGUOUS,
})

# Occasion / safety categories that remain on deterministic templates
# (P1-D-3 occasion gate). All other social warmth → LLM compose.
TEMPLATE_ONLY_SOCIAL_CATEGORIES = frozenset({
    "eid_greeting",
    "dua",
    "religious_media",
    "condolence",
})

# Shared negative guidance — behavioral only, not reply text.
_NO_SERVICE_CLOSER = (
    "Do NOT end with customer-service or help-desk closers — no "
    "«كيف أقدر أخدمك», «كيف أساعدك», «أنا هنا للمساعدة», "
    "«إذا احتجت أي مساعدة», «خبرني كيف أساعدك», or «تحت أمرك» as a "
    "closing line. End on the social beat; do not pivot to assistance."
)

_KIND_GUIDANCE: dict[str, str] = {
    "affection": (
        "Energy: warm reciprocal — acknowledge the feeling modestly in Saudi "
        "tone; no romantic escalation, no sales pivot, no support boilerplate. "
        "No help-offer ending."
    ),
    "appearance": (
        "Energy: modest friendly acknowledgment — light deflection or "
        "kindness mirror; no over-flattery, no poetic Gulf-generic lines. "
        "No service framing or assistance offer at the end."
    ),
    "tease": (
        "Energy: light playful pushback — match tease with tease, not apology "
        "plus support tone; humor and mild comeback are welcome. "
        "No customer-service recovery pattern."
    ),
    "upset": (
        "Energy: gentle light repair — acknowledge without groveling; no "
        "support-ticket tone, staff escalation promise, or discount offer. "
        "No escalation or handoff language."
    ),
    "greeting": (
        "Energy: warm phatic reciprocity — the customer sent a short hello "
        "or re-greeting. Match their greeting energy naturally in 1–3 short "
        "lines. Do NOT self-introduce on ANY greeting turn — even when "
        "identity_already_introduced=false (no «أنا نحلة», no assistant "
        "role, no capability bullets, no onboarding lists). This is not an "
        "identity FAQ and not a sales opening — do not pivot to catalog, "
        "checkout, or «how can I help» framing."
    ),
    "social": (
        "Energy: warm conversational Saudi personality — natural and human, "
        "not merchant FAQ or sales assistant."
    ),
}


def is_established_greet_persona_compose_enabled() -> bool:
    """Kill switch for established-greeting persona compose (default ON).

    When OFF, the decision engine falls back to legacy ``ACTION_GREET`` +
    ``re_greet`` templates for rollback only — not the primary personality path.
    """
    raw = str(os.getenv(_ESTABLISHED_GREET_PERSONA_FLAG, "true")).strip().lower()
    if raw in ("", "1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return True

_PERSONA_OMIT_STATE_JSON_KEYS = (
    "recommended_next_step",
    "coupon_policy",
    "last_recommended_products",
    "selected_product",
    "explicit_pending_action",
    "policy_reason",
)


def compose_non_sales_ambiguous_goal() -> str:
    return (
        "non_sales_ambiguous — Generate a short natural Saudi Arabic WhatsApp "
        "reply to a conversational or phatic turn that carries no product, "
        "order, price, payment, shipping, or catalog request. "
        "Match the customer's energy warmly in 1–3 short lines — "
        "conversational, not merchant FAQ, not a sales assistant. "
        "Do NOT pitch products, prices, checkout, or catalog items. "
        "Do NOT ask disambiguation-menu questions about which product or "
        "specification they want when they have not asked to buy. "
        "Do NOT use rigid FAQ phrasing such as «تحت أمرك» as the whole reply. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]. "
        f"{_NO_SERVICE_CLOSER}"
    )


def persona_topic_from_decision_args(args: Optional[dict]) -> str:
    """Return a persona compose topic token or ``""``."""
    topic = str((args or {}).get("topic") or "").strip()
    if topic in PERSONA_TOPICS:
        return topic
    return ""


def is_template_only_social_category(category: str) -> bool:
    return (category or "").strip().lower() in TEMPLATE_ONLY_SOCIAL_CATEGORIES


def build_social_courtesy_decision(
    category: str,
    *,
    confidence: float,
    reason: str,
    block_commerce: bool = False,
    extra_args: Optional[dict] = None,
) -> Any:
    """Route social courtesy to LLM persona compose or template-only safety."""
    from modules.ai.brain.decision.actions import (  # noqa: PLC0415
        ACTION_LLM_REPLY,
        ACTION_SOCIAL_REPLY,
    )
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    cat = (category or "general_courtesy").strip().lower() or "general_courtesy"
    args: dict[str, Any] = dict(extra_args or {})
    if block_commerce:
        args["block_commerce_escalation"] = True

    if is_template_only_social_category(cat):
        args.setdefault("social_category", cat)
        return Decision(
            action=ACTION_SOCIAL_REPLY,
            args=args,
            reason=reason,
            confidence=confidence,
        )

    args.setdefault("topic", PERSONA_TOPIC_SOCIAL_PERSONA_ACK)
    args.setdefault("social_category", cat)
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=reason,
        confidence=confidence,
    )


_SOCIAL_CATEGORY_CONTEXT: dict[str, str] = {
    "thanks": "The customer is thanking you.",
    "blessing": "The customer sent a brief blessing or dua.",
    "strong_praise": (
        "The customer gave explicit strong praise of the shop, service, or merchant."
    ),
    "compliment": "The customer complimented the shop or service.",
    "general_courtesy": "The customer sent a short courtesy or phatic message.",
    "emotional_personal": "The customer sent a warm personal or emotional message.",
    "prophet_invocation": "The customer invoked blessings on the Prophet.",
    "basmala": "The customer opened with basmala.",
    "social_forward": "The customer forwarded or shared a social message.",
    "morning_greeting": "The customer sent a morning greeting.",
    "celebration": "The customer shared a celebration or congratulation.",
    "informational_only": "The customer sent a brief social acknowledgment.",
}


def compose_social_persona_goal(social_category: str) -> str:
    """Principle-based response goal for personality social turns (P1-F)."""
    cat = str(social_category or "social").strip() or "social"
    ctx_note = _SOCIAL_CATEGORY_CONTEXT.get(
        cat,
        "The customer sent a short social or phatic message.",
    )
    return (
        f"social_persona_ack — {ctx_note} "
        "Compose a short natural Saudi Arabic WhatsApp reply. "
        "Principles: respond naturally in Saudi tone; warm but not poetic; "
        "one social beat is enough unless the context clearly needs slightly "
        "more; do not stack multiple prayers or duas; match the customer's "
        "tone and context; vary wording naturally across turns. "
        "Do not add customer-service or sales closers. "
        "Do not invent operational facts. "
        "Do not mention products, orders, payment, or shipping unless the "
        "customer's message actually requires it. "
        f"{_NO_SERVICE_CLOSER}"
    )


def is_persona_expression_topic(topic: str) -> bool:
    return str(topic or "").strip() in PERSONA_TOPICS


def persona_kind_guidance(persona_kind: str) -> str:
    key = str(persona_kind or "social").strip().lower() or "social"
    return _KIND_GUIDANCE.get(key, _KIND_GUIDANCE["social"])


def compose_persona_identity_goal() -> str:
    return (
        "persona_identity — Generate a short natural Saudi Arabic WhatsApp "
        "reply. The customer is asking who you are, whether you are Nahla, "
        "a bot, AI, or human, or is playfully probing (e.g. «تنامين؟»). "
        "Answer in Nahla's warm playful persona: 1–3 short lines, "
        "conversational Saudi tone, emotionally natural — not support "
        "boilerplate. "
        "For sleep/playful probes: banter naturally as Nahla — avoid "
        "system/support phrasing and avoid «digital assistant always "
        "available» boilerplate. "
        "Do NOT use onboarding bullet lists or enumerate product/price/"
        "shipping/order capabilities. "
        "Do NOT pitch products, prices, checkout, or catalog items. "
        "Do NOT use rigid FAQ phrasing such as «تحت أمرك» as the whole "
        "reply or «نظام ذكاء اصطناعي» boilerplate. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]. "
        f"{_NO_SERVICE_CLOSER}"
    )


def compose_persona_social_goal(persona_kind: str) -> str:
    pk = str(persona_kind or "social").strip() or "social"
    guidance = persona_kind_guidance(pk)
    return (
        f"persona_social — Generate a short natural Saudi Arabic WhatsApp "
        f"reply to a social/personality message (persona_kind={pk}). "
        f"{guidance} "
        "Respond in 1–3 short lines — not support boilerplate, not a sales "
        "pitch. "
        "Do NOT pitch products, prices, checkout, or catalog items. "
        "Do NOT use onboarding bullet lists or enumerate store capabilities. "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…]. "
        "Do NOT use rigid FAQ phrasing such as «تحت أمرك» as the whole reply. "
        f"{_NO_SERVICE_CLOSER}"
    )


def build_persona_residual_rules(*, tone: str) -> str:
    """Platform-wide residual rules for persona compose turns (Phase 3B)."""
    tone_label = tone or "neutral"
    return (
        "## قواعد تشغيل Brain — جولة شخصية/اجتماعية (persona)\n"
        f"- النبرة: {tone_label} — سعودية طبيعية، دافئة، مختصرة.\n"
        "- اتبعي **response_goal** أعلاه — يتجاوز stage وأي إشارة JSON "
        "تجارية أو خطوة بيعية.\n"
        "- هذه جولة **شخصية/اجتماعية** — لا منتجات، لا أسعار، لا checkout، "
        "لا كوبونات، لا طلب عنوان، لا تصعيد موظف.\n"
        "- **ممنوع** إغلاق الرد بعبارات خدمة عملاء أو مكتب مساعدة: "
        "«كيف أقدر أخدمك»، «كيف أساعدك»، «أنا هنا للمساعدة»، "
        "«إذا احتجت أي مساعدة»، «خبرني كيف أساعدك»، «تحت أمرك» كختام.\n"
        "- انهي الرد على نغمة المحادثة — لا سؤال متابعة بيعي ولا CTA مساعدة.\n"
        "- لا تخترعي حقائق. لا [PRODUCT:…] / [MEDIA_KEY:…].\n"
        "- اجعلي الرد 1–3 أسطر (راجع HIGH PRIORITY)."
    )


def build_persona_json_footer(*, brain_state_json: str) -> str:
    return (
        f"BrainStateJSON:\n{brain_state_json}\n\n"
        "جولة شخصية/اجتماعية: استخدمي JSON لفهم **استمرارية المحادثة** فقط "
        "(recent_turns، conversation_summary، customer_memory، greeted، "
        "identity_already_introduced). "
        "**ممنوع** اقتراح خطوة تجارية أو checkout أو pitch بيعي بناءً على JSON."
    )


def slim_brain_state_dict_for_persona(
    state_dict: Dict[str, Any],
    *,
    persona_topic: str = "",
) -> Dict[str, Any]:
    """Drop commerce progression keys from JSON surfaced to Claude on persona turns."""
    from modules.ai.prompts.high_priority_layer import (  # noqa: PLC0415
        filter_owner_instructions_for_persona,
    )

    topic = str(persona_topic or "").strip()
    out = dict(state_dict)
    for key in _PERSONA_OMIT_STATE_JSON_KEYS:
        out.pop(key, None)
    mc = out.get("merchant_context")
    if isinstance(mc, dict):
        slim_mc = dict(mc)
        slim_mc.pop("resolver_overlay", None)
        slim_mc.pop("structured_facts_block", None)
        ai = slim_mc.get("ai_settings")
        if isinstance(ai, dict):
            slim_ai = dict(ai)
            slim_ai.pop("assistant_role", None)
            if topic != PERSONA_TOPIC_IDENTITY:
                slim_ai.pop("assistant_name", None)
            owner_raw = str(slim_ai.get("owner_instructions") or "").strip()
            if owner_raw:
                filtered = filter_owner_instructions_for_persona(owner_raw)
                if filtered:
                    slim_ai["owner_instructions"] = filtered
                else:
                    slim_ai.pop("owner_instructions", None)
            slim_mc["ai_settings"] = slim_ai
        out["merchant_context"] = slim_mc
    return out


__all__ = [
    "PERSONA_KIND_GREETING",
    "PERSONA_KIND_GUIDANCE",
    "PERSONA_TOPIC_IDENTITY",
    "PERSONA_TOPIC_SOCIAL",
    "PERSONA_TOPIC_SOCIAL_PERSONA_ACK",
    "PERSONA_TOPIC_NON_SALES_AMBIGUOUS",
    "PERSONA_TOPICS",
    "TEMPLATE_ONLY_SOCIAL_CATEGORIES",
    "is_established_greet_persona_compose_enabled",
    "build_persona_json_footer",
    "build_persona_residual_rules",
    "build_social_courtesy_decision",
    "compose_non_sales_ambiguous_goal",
    "compose_persona_identity_goal",
    "compose_persona_social_goal",
    "compose_social_persona_goal",
    "is_persona_expression_topic",
    "is_template_only_social_category",
    "persona_kind_guidance",
    "persona_topic_from_decision_args",
    "slim_brain_state_dict_for_persona",
]

# Exported for tests that assert keys exist.
PERSONA_KIND_GUIDANCE = _KIND_GUIDANCE
