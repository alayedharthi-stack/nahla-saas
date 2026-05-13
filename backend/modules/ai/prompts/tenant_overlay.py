"""
prompts/tenant_overlay.py
─────────────────────────
Tenant Assistant Settings → Prompt Overlay.

Converts the merchant's dashboard AI settings (stored in TenantSettings.ai_settings
JSONB) into a stable, structured prompt block that is injected into the existing
system prompt — without replacing or restructuring the base prompt.

Design constraints:
  - Pure normalization layer: maps UI-friendly values to stable model instructions.
  - Safe fallback: returns "" when settings are absent, so the AI behaves exactly
    as before for tenants without customized settings.
  - Non-breaking: injected alongside (not instead of) the base system prompt.
  - No provider selection, routing, or fallback logic changes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.ai.overlay")

# ── Normalization maps ────────────────────────────────────────────────────────
# Keys support both English enum values (stored in DB) and Arabic UI labels
# that merchants may set in future UI iterations.

TONE_MAP: Dict[str, str] = {
    "friendly":  "ودي وطبيعي — تحدث كصديق ينصح بصدق، لا كموظف يبيع بأي ثمن.",
    "formal":    "رسمي ومحترم — استخدم لغة مهنية وألقاب احترام مع كل عميل.",
    "casual":    "عفوي ومرح — تحدث بأسلوب يومي خفيف كأنك تكلّم صاحب.",
    "brief":     "مختصر ومباشر — أقل كلام ممكن لتوصيل المعلومة بوضوح.",
    "neutral":   "متوازن ومهني — ودود لكن بدون مبالغة في الألفة.",
    "ودية وقريبة":    "ودي وطبيعي — تحدث كصديق ينصح بصدق، لا كموظف يبيع بأي ثمن.",
    "رسمية ومحترمة":  "رسمي ومحترم — استخدم لغة مهنية وألقاب احترام مع كل عميل.",
    "مرحة وخفيفة":    "عفوي ومرح — تحدث بأسلوب يومي خفيف كأنك تكلّم صاحب.",
    "مختصرة ومباشرة": "مختصر ومباشر — أقل كلام ممكن لتوصيل المعلومة بوضوح.",
}

LANGUAGE_MAP: Dict[str, str] = {
    "arabic": (
        "تحدث بالعربية واللهجة السعودية العامية الصحيحة دائماً. "
        "إذا بدأ العميل بالإنجليزية أو طلب التحدث بالإنجليزية، انتقل للإنجليزية."
    ),
    "english": (
        "Reply in English. Switch to Arabic only if the customer explicitly "
        "requests it or writes in Arabic."
    ),
    "bilingual": (
        "تحدث بنفس لغة العميل — إذا كتب بالعربية ردّ بالعربية، وإذا كتب "
        "بالإنجليزية ردّ بالإنجليزية. يمكنك مزج اللغتين إذا العميل يفعل ذلك."
    ),
    "عربي":          "تحدث بالعربية واللهجة السعودية العامية دائماً.",
    "إنجليزي":       "Reply in English only.",
    "ثنائي اللغة":   "تحدث بنفس لغة العميل — عربي يردّ عربي، إنجليزي يردّ إنجليزي.",
}

LENGTH_MAP: Dict[str, str] = {
    "short":  "ردودك قصيرة جداً — جملة إلى جملتين كحد أقصى. لا شرح إضافي إلا إذا طُلب صراحةً.",
    "medium": "ردودك متوسطة — 3 إلى 4 أسطر كحد أقصى. اختصر دائماً.",
    "long":   "يمكنك الرد بتفصيل عند الحاجة — لكن لا تتجاوز 6 أسطر في الغالب.",
    "قصير":  "ردودك قصيرة جداً — جملة إلى جملتين كحد أقصى.",
    "متوسط": "ردودك متوسطة — 3 إلى 4 أسطر كحد أقصى. اختصر دائماً.",
    "مفصل":  "يمكنك الرد بتفصيل عند الحاجة — لكن لا تتجاوز 6 أسطر.",
}


def build_tenant_overlay_split(
    settings: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Split a tenant's ai_settings into the three architectural buckets:

        {
          "identity": "<اسم/دور المساعد>",          # neutral, free-form
          "style":    "<style block contents>",      # → consumed by High-Priority layer
          "policy":   "<policy block contents>",     # → consumed by High-Priority layer
          "facts":    "<facts-only KB body>",        # → consumed by KB block
        }

    Keys are always present (empty string when no data). This is the
    primary feed for the new 3-layer prompt structure introduced in
    Phase 1 of the prompt-pipeline refactor. The legacy single-string
    overlay (`build_tenant_prompt_overlay`) is now a thin wrapper that
    concatenates these buckets for callers that haven't migrated yet.
    """
    buckets = {"identity": "", "style": "", "policy": "", "facts": ""}
    if not settings:
        return buckets

    # ── identity ──────────────────────────────────────────────────────────
    name = str(settings.get("assistant_name") or "").strip()
    role = str(settings.get("assistant_role") or "").strip()
    if name or role:
        identity_lines: list[str] = []
        if name:
            identity_lines.append(f"- اسمك: {name}")
        if role:
            identity_lines.append(f"- دورك: {role}")
        buckets["identity"] = "هوية المساعد:\n" + "\n".join(identity_lines)

    # ── style (tone + language + length) ──────────────────────────────────
    style_parts: list[str] = []
    tone_key = str(settings.get("reply_tone") or "").strip()
    tone_instruction = TONE_MAP.get(tone_key)
    if tone_instruction:
        style_parts.append(f"النبرة المطلوبة: {tone_instruction}")
    lang_key = str(settings.get("default_language") or "").strip()
    lang_instruction = LANGUAGE_MAP.get(lang_key)
    if lang_instruction:
        style_parts.append(f"لغة الرد: {lang_instruction}")
    length_key = str(settings.get("reply_length") or "").strip()
    length_instruction = LENGTH_MAP.get(length_key)
    if length_instruction:
        style_parts.append(f"طول الرد: {length_instruction}")
    if style_parts:
        buckets["style"] = "\n\n".join(style_parts)

    # ── policy (owner_instructions + coupons + escalation) ────────────────
    policy_parts: list[str] = []
    owner_instructions = str(settings.get("owner_instructions") or "").strip()
    if owner_instructions:
        policy_parts.append(f"تعليمات صاحب المتجر:\n{owner_instructions}")
    coupon_rules = str(settings.get("coupon_rules") or "").strip()
    allowed_discount = str(settings.get("allowed_discount_levels") or "").strip()
    if coupon_rules or allowed_discount:
        disc_lines: list[str] = []
        if coupon_rules:
            disc_lines.append(coupon_rules)
        if allowed_discount:
            disc_lines.append(f"- الحد الأقصى المسموح للخصم: {allowed_discount}%")
        policy_parts.append(
            "قواعد الخصومات والكوبونات:\n" + "\n".join(disc_lines)
        )
    escalation_rules = str(settings.get("escalation_rules") or "").strip()
    if escalation_rules:
        policy_parts.append(f"قواعد التحويل والتصعيد:\n{escalation_rules}")
    if policy_parts:
        buckets["policy"] = "\n\n".join(policy_parts)

    # ── facts (manual_knowledge_base — KB only, no behavior) ──────────────
    # CRITICAL DESIGN RULE — do NOT collapse this into owner_instructions:
    #   * owner_instructions       = how the assistant *behaves*  (→ policy)
    #   * manual_knowledge_base    = facts the assistant can cite (→ facts)
    # The block is tagged as a non-authoritative source for prices/inventory
    # so that Salla-synced data (loaded via core.store_knowledge.build_
    # merchant_context) always wins on those fields, even if the merchant
    # accidentally pasted stale prices in here.
    knowledge_base = str(settings.get("manual_knowledge_base") or "").strip()
    if knowledge_base:
        buckets["facts"] = (
            "قاعدة المعرفة (معلومات المتجر — Facts فقط):\n"
            f"{knowledge_base}\n\n"
            "ملاحظات لاستخدام قاعدة المعرفة:\n"
            "- استخدم هذه المعلومات للإجابة على أسئلة العملاء عن المنتجات "
            "والشحن والضمان والأسئلة الشائعة وأي تفاصيل أضافها التاجر هنا.\n"
            "- إذا كان المتجر مربوطًا بسلة فإن السعر، التوفر، المخزون، "
            "المتغيرات، ورابط المنتج المباشر تأتي من بيانات سلة في "
            "merchant_context وهي المصدر الرسمي — لا تستخدم أي رقم سعر أو "
            "حالة توفر من هذه القاعدة لمخالفة بيانات سلة.\n"
            "- إذا تعارض السعر هنا مع سعر سلة، اعتمد سعر سلة دائمًا ولا "
            "تذكر السعر اليدوي.\n"
            "- لا تختلق معلومات ليست في القاعدة أو في merchant_context."
        )

    return buckets


def build_tenant_prompt_overlay(settings: Optional[Dict[str, Any]]) -> str:
    """
    Backward-compatible wrapper.

    Returns the legacy single-string overlay by concatenating the
    output of `build_tenant_overlay_split`. New callers should consume
    the split dict directly so the High-Priority layer can pull style +
    policy out of the system prompt and into the priority banner.

    Returns "" if settings is None/empty — this preserves current AI behavior
    for tenants that have not customized their assistant.
    """
    if not settings:
        return ""

    buckets = build_tenant_overlay_split(settings)
    sections: list[str] = []
    if buckets["identity"]:
        sections.append(buckets["identity"])
    if buckets["style"]:
        sections.append(buckets["style"])
    if buckets["policy"]:
        sections.append(buckets["policy"])
    if buckets["facts"]:
        sections.append(buckets["facts"])

    if not sections:
        return ""

    return "\n\n".join([
        "═══ إعدادات مساعد المتجر (تُطبّق بأولوية عالية) ═══",
        *sections,
        "═══ نهاية إعدادات المتجر ═══",
    ])


def load_tenant_ai_overlay(db: Any, tenant_id: int) -> str:
    """
    Load tenant AI settings from DB, merge with defaults, and return
    the rendered prompt overlay string.

    Safe: returns "" on any error so the AI pipeline never breaks.
    """
    try:
        from models import TenantSettings  # noqa: PLC0415
        from core.tenant import merge_ai_defaults  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if not ts:
            return ""

        settings = merge_ai_defaults(ts.ai_settings)
        return build_tenant_prompt_overlay(settings)
    except Exception as exc:
        logger.warning(
            "[overlay] Failed to load AI settings for tenant=%s: %s",
            tenant_id, exc,
        )
        return ""


def get_tenant_tone(db: Any, tenant_id: int) -> str:
    """
    Return the normalized tone key for the Brain prompt builder.

    Falls back to "neutral" on any failure.
    """
    try:
        from models import TenantSettings  # noqa: PLC0415
        from core.tenant import merge_ai_defaults  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if ts:
            settings = merge_ai_defaults(ts.ai_settings)
            return str(settings.get("reply_tone") or "neutral")
    except Exception:
        pass
    return "neutral"
