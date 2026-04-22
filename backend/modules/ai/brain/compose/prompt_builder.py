"""
brain/compose/prompt_builder.py
───────────────────────────────
Short prompt builder for MerchantBrain LLM fallback.

Unlike the legacy prompt builder, this module does not try to encode business
logic as dozens of patch rules.  It only defines:
  - the role
  - the goal
  - the tone
  - one general coupon / discount rule
  - one anti-repetition rule
  - the requirement to follow the current customer stage

The actual conversational context is injected as structured `BrainReplyState`.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from modules.ai.prompts.nahla_persona import nahla_persona_system_prompt

from ..types import BrainReplyState


def build_brain_reply_prompt(state: BrainReplyState) -> str:
    """Compose the LLM prompt for Brain's `compose_reply` fallback.

    Layering (top → bottom):
      1. Nahla persona — the canonical voice / emoji rules (shared with
         the legacy WhatsApp AI path so both engines sound identical).
      2. Tenant overlay — merchant-specific tone / rules, allowed to
         override the default persona.
      3. Brain-specific operating rules — stage following, anti-repeat,
         coupon discipline, no-hallucination guard.
      4. Structured BrainState JSON — the actual conversation context.
    """
    store_name = state.store_name or "المتجر"
    tone = _tone_instruction(state.tone)

    # Strip tenant_overlay from the JSON to avoid duplication — it is
    # rendered separately above the state block for higher priority.
    state_dict = asdict(state)
    overlay_text = state_dict.pop("tenant_overlay", "")
    brain_state_json = json.dumps(state_dict, ensure_ascii=False, indent=2)

    parts = [nahla_persona_system_prompt(store_name=store_name)]

    if overlay_text:
        parts.append(overlay_text)

    # Surface the four "must-have" fields the decision pipeline guarantees
    # — intent, stage, current product, response goal — so the LLM never
    # has to guess WHY it's being asked to compose this turn.
    selected_title = ""
    if isinstance(state.selected_product, dict):
        selected_title = str(state.selected_product.get("title") or "")
    decision_block = (
        "## سياق القرار لهذه الجولة (مصدر واحد فقط)\n"
        f"- الـ intent الحالي: {state.intent_name or 'unknown'}\n"
        f"- المرحلة (stage): {state.stage}\n"
        f"- المنتج الحالي: {selected_title or '—'}\n"
        f"- هدف الرد (response_goal): {state.response_goal or '—'}\n"
        "هذا السياق هو الحقيقة الرسمية للجولة — لا تتجاوزيه ولا تعيدي "
        "تشخيص نية العميل من نص الرسالة وحدها."
    )

    parts.append(
        decision_block + "\n\n"
        "## قواعد تشغيل Brain لهذه الجولة\n"
        f"- النبرة المطلوبة لهذا المتجر: {tone}.\n"
        "- اتبعي المرحلة (stage) والخطوة المقترحة التالية (recommended_next_step) "
        "وهدف الرد (response_goal) أعلاه — حتى لو شعرتِ أن الرسالة تستحق رداً مختلفاً.\n"
        "- إذا كانت المرحلة ordering/checkout فلا ترحبي ولا تعيدي التعريف بنفسك "
        "ولا تعرضي قائمة منتجات جديدة — أكملي الطلب الحالي بسؤال واحد قصير.\n"
        "- لا تكرّري نفس السؤال إذا كان قد طُرح بالفعل وكان الجواب معروفاً.\n"
        "- لا تعرضي خصماً أو كوبوناً إلا إذا أظهر BrainState أن الوقت "
        "مناسب أو طلب العميل خصماً بوضوح. (عند ذكر الخصم استخدمي 🎁 "
        "بحد أقصى مرة واحدة في الرسالة.)\n"
        "- إذا كانت المعلومة ناقصة، اسألي سؤال متابعة واحداً فقط، قصيراً وواضحاً.\n"
        "- لا تخترعي حقائق غير موجودة في known_facts أو selected_product.\n"
        "- اجعلي ردك قصيراً ومناسباً لواتساب.\n\n"
        "BrainStateJSON:\n"
        f"{brain_state_json}\n\n"
        "إذا كانت conversation_summary أو customer_memory أو store_knowledge "
        "موجودة فاستخدميها لفهم السياق واقتراح الخطوة التجارية التالية، "
        "لكن لا تذكري أي معلومة غير موجودة فيها."
    )

    return "\n\n".join(parts)


def _tone_instruction(tone: str) -> str:
    tone_map = {
        "formal": "رسمي ومحترم",
        "casual": "ودي ومريح",
        "brief": "مختصر جداً",
        "neutral": "ودود ومهني وواضح",
    }
    return tone_map.get(tone or "neutral", tone_map["neutral"])
