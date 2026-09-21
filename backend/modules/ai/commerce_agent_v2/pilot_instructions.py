"""The commerce-runtime pilot's instruction assembly.

The merchant instructions are owned by this module and stay exactly as they are:
``COMMERCE_AGENT_INSTRUCTIONS`` is imported and never edited, and the Agents-SDK
agent keeps using it unchanged.

Two statements in it are nevertheless *false* for the commerce-runtime pilot, and
leaving them in place would be the opposite of reuse — it would be telling the
model something untrue about the run it is in:

1. It instructs the model to answer by filling ``CommerceReply`` fields
   (``fact_claims``, ``response_mode``, ``safe_fallback_reason`` and the rest).
   The pilot has no such object: the model answers through one declared tool.
2. It says the phase is read-and-evaluate in shadow mode. On the pilot the reply
   is delivered to a real person.

``PILOT_REPLY_ADDENDUM`` corrects exactly those two things and nothing else. It
adds no tone, no persona, no greeting and no wording: every truth rule, every
tool rule and every grounding rule above it is unchanged and still binding. The
tool names it refers to are the ones the instructions already use, and the
registry declares those same names — the two are asserted equal by test.

This is a narrow, documented, pilot-only adaptation. It is not applied to the
legacy path, and ``COMMERCE_AGENT_INSTRUCTIONS`` is not modified.
"""
from __future__ import annotations

from typing import Tuple

from modules.ai.commerce_agent_v2.agent import COMMERCE_AGENT_INSTRUCTIONS

# The tool names the instructions above already refer to. The live registry must
# declare exactly these; a rename on either side without the other leaves the
# model calling a tool that does not exist.
INSTRUCTION_TOOL_NAMES: Tuple[str, ...] = (
    "search_products",
    "get_product_details",
    "search_merchant_knowledge",
    "resolve_customer_order",
    "get_order_details",
    "get_order_shipment",
)

PILOT_ADDENDUM_HEADING = "— تشغيل Nahlah Commerce Runtime (هذا التشغيل فقط) —"

PILOT_REPLY_ADDENDUM = """
— تشغيل Nahlah Commerce Runtime (هذا التشغيل فقط) —

هذان البندان يصححان ما يخص هذا التشغيل فقط. كل ما سبق من قواعد الحقيقة والأدوات
والاستناد إلى evidence يبقى ساريًا كما هو.

1) صيغة الرد: لا يوجد كائن CommerceReply في هذا التشغيل. سلّم إجابتك النهائية عبر
   استدعاء الأداة submit_reply مرة واحدة فقط ووحدها، بالحقول التالية:
   - text: نص الرد للعميل بلغته.
   - evidence_refs: قائمة بمراجع evidence_ref التي أعادتها أدوات هذا التشغيل
     وتستند إليها إجابتك.
   - claims_commerce_facts: true إذا ذكر النص أي حقيقة عن منتج أو سعر أو توفر أو
     طلب أو شحنة أو معرفة المتجر، وfalse للرد المحادثاتي البحت.
   لا ترسل نصًا خارج submit_reply، ولا تستدعِ submit_reply مع أدوات أخرى في نفس
   الخطوة.

2) التسليم: ردك في هذا التشغيل يُرسل فعليًا إلى العميل عبر واتساب. ليست مرحلة
   shadow. تبقى كل القيود كما هي: أدوات القراءة فقط، ولا تنفّذ طلبًا أو دفعًا أو
   إلغاءً أو أي تغيير، ولا ترسل بنفسك — المنصة هي التي تُسلّم.
""".strip()


def build_pilot_instructions() -> str:
    """The merchant instructions verbatim, plus the pilot-only correction."""
    base = str(COMMERCE_AGENT_INSTRUCTIONS or "").strip()
    if not base:
        raise ValueError("the commerce agent instructions are empty; the pilot composes none")
    return f"{base}\n\n{PILOT_REPLY_ADDENDUM}\n"


__all__ = [
    "INSTRUCTION_TOOL_NAMES", "PILOT_ADDENDUM_HEADING", "PILOT_REPLY_ADDENDUM",
    "build_pilot_instructions",
]
