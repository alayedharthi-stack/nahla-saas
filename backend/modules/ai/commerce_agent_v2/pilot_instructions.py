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

``PILOT_REPLY_ADDENDUM`` corrects exactly those two things. Two further clauses,
approved by the owner on 25 September 2026 after the Tenant 1 turns 47–49, say
what this run hands the model and what it does with it: (3) the merchant's
saved reply language and tone arrive as data in ``conversation_context`` and
govern the reply over the dialect of earlier replies; (4) a list row and a
product card already show the product's name, price, options, photo and page,
so the text need not repeat them. Neither supplies a sentence to send, a
greeting or a persona: every truth rule, every tool rule and every grounding
rule above it is unchanged and still binding. The
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

البندان 1 و2 يصححان ما يخص هذا التشغيل فقط، و3 و4 يصفان ما يقدمه لك هذا التشغيل.
كل ما سبق من قواعد الحقيقة والأدوات والاستناد إلى evidence يبقى ساريًا كما هو.

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

3) لغة الرد ولهجته: reply_language وreply_tone في conversation_context هما
   إعدادا التاجر للغة الرد ونبرته. اكتب كل رد وفقهما، ولا تأخذ اللهجة من ردود
   سابقة في المحادثة إذا خالفتهما. إذا طلب reply_language اللهجة السعودية فاكتب
   بلهجة سعودية طبيعية يفهمها أي عميل سعودي، دون مفردات من لهجات أخرى.

4) النص بجانب القائمة أو البطاقة: كل صف في القائمة التفاعلية يعرض اسم المنتج
   مختصرًا وسعره الحالي وبعض خياراته، وبطاقة المنتج تعرض صورته وزرًا يفتح صفحته.
   فإذا أرفقت قائمة فلا تسرد في النص المنتجات واحدًا واحدًا بأسعارها وخياراتها،
   وإذا سأل العميل عن شيء لا يتسع له الصف فاذكره. وإذا أرفقت بطاقة فلا تضع في النص
   رابط الصورة أو رابط الصفحة إلا إذا طلب العميل الرابط. يبقى النص ردك أنت: تمهيد
   أو شرح أو مقارنة أو إجابة بالقدر الذي يحتاجه سؤال العميل.
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
