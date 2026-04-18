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

from ..types import BrainReplyState


def build_brain_reply_prompt(state: BrainReplyState) -> str:
    store_name = state.store_name or "المتجر"
    tone = _tone_instruction(state.tone)
    brain_state_json = json.dumps(asdict(state), ensure_ascii=False, indent=2)

    return (
        f"أنت مساعد مبيعات ذكي على واتساب لمتجر \"{store_name}\".\n"
        "هدفك أن تفهم سياق العميل بسرعة، وتساعده بشكل طبيعي، وتقوده للخطوة التالية المناسبة نحو الشراء أو الخدمة.\n"
        f"النبرة المطلوبة: {tone}\n"
        "قواعد عامة:\n"
        "- اتبع مرحلة العميل الحالية (stage) والخطوة المقترحة التالية (recommended_next_step).\n"
        "- لا تكرر نفس السؤال إذا كان قد طُرح بالفعل وكان الجواب معروفاً أو غير لازم.\n"
        "- لا تعرض خصماً أو كوبوناً إلا إذا أظهر Brain State أن الوقت مناسب أو طلب العميل خصماً بوضوح.\n"
        "- إذا كانت المعلومة ناقصة، اسأل سؤال متابعة واحداً فقط، قصيراً وواضحاً.\n"
        "- لا تخترع حقائق غير موجودة في known_facts أو selected_product.\n"
        "- اجعل ردك قصيراً ومناسباً لواتساب.\n\n"
        "BrainStateJSON:\n"
        f"{brain_state_json}"
    )


def _tone_instruction(tone: str) -> str:
    tone_map = {
        "formal": "رسمي ومحترم",
        "casual": "ودي ومريح",
        "brief": "مختصر جداً",
        "neutral": "ودود ومهني وواضح",
    }
    return tone_map.get(tone or "neutral", tone_map["neutral"])
