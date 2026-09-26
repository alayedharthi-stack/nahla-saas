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
so the text need not repeat them. On 26 September 2026 the owner approved
extending clause 3 to follow the separate Arabic-dialect setting
(stored in the tenant settings' metadata, delivered as ``reply_dialect`` in the meaning
``core.reply_dialect`` defines): the merchant's current settings are the
reference for language and dialect, however earlier replies were written;
``reply_language`` alone decides Arabic or English; and the dialect governs
Arabic replies only. Also on 26 September 2026, after tenant 33 turn 68 (a
personal message searched as a product name, then answered "no such product"),
the owner approved one general clause (5) for a message that is not clearly a
product request: read it in the conversation's context before searching; a
clear product request is searched and answered without asking; a message that
may mean something else is clarified, not treated as a product request; and a
search that matched nothing for such a phrase says only that the text matched
no product. It names no phrase and supplies no reply. No clause supplies a sentence to send, a greeting or
a persona: every truth rule, every tool rule and every grounding rule above it
is unchanged and still binding. The tool names it refers to are the ones the
instructions already use, and the registry declares those same names — the two
are asserted equal by test.

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

البندان 1 و2 يصححان ما يخص هذا التشغيل فقط، و3 و4 يصفان ما يقدمه لك هذا التشغيل،
و5 يخص الرسالة التي لا يتضح أنها طلب منتج.
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

3) لغة الرد ولهجته: reply_language وreply_dialect وreply_tone في
   conversation_context هي إعدادات التاجر الحالية للغة الرد ولهجته العربية
   ونبرته. اكتب كل رد وفقها، ولا تأخذ اللغة أو اللهجة من ردود سابقة في
   المحادثة إذا خالفتها. reply_language يحدد متى تردّ بالعربية ومتى
   بالإنجليزية. إذا وُجد reply_dialect فاتبعه في كل رد عربي؛ وإن لم يوجد وطلب
   reply_language اللهجة السعودية فاكتب بلهجة سعودية طبيعية يفهمها أي عميل
   سعودي. لا تخلط مفردات من لهجات أخرى، واللهجة لا تغيّر لغة الرد: إذا كان الرد
   بالإنجليزية فاكتبه بالإنجليزية.

4) النص بجانب القائمة أو البطاقة: كل صف في القائمة التفاعلية يعرض اسم المنتج
   مختصرًا وسعره الحالي وبعض خياراته، وبطاقة المنتج تعرض صورته وزرًا يفتح صفحته.
   فإذا أرفقت قائمة فلا تسرد في النص المنتجات واحدًا واحدًا بأسعارها وخياراتها،
   وإذا سأل العميل عن شيء لا يتسع له الصف فاذكره. وإذا أرفقت بطاقة فلا تضع في النص
   رابط الصورة أو رابط الصفحة إلا إذا طلب العميل الرابط. يبقى النص ردك أنت: تمهيد
   أو شرح أو مقارنة أو إجابة بالقدر الذي يحتاجه سؤال العميل.

5) الرسالة التي لا يتضح أنها طلب منتج: ليست كل رسالة تصلك سؤالًا عن منتج؛ قد تكون
   تحية أو كلامًا شخصيًا موجّهًا لصاحب الرقم أو عبارة تحتمل أكثر من معنى. افهم
   الرسالة في سياق المحادثة قبل أن تبحث. إذا كان واضحًا أن العميل يسأل عن منتج أو
   يتصفح المنتجات فابحث وأجب مباشرة دون أن تستوضح. وإذا احتملت الرسالة معنى غير طلب
   منتج فاستوضح قصده بكلامك، ولا تعاملها كطلب منتج ولا تعرض عليه منتجات بدل
   الاستيضاح. وإذا بحثت بعبارة لم يتضح أنها اسم منتج فلم تطابق شيئًا، فذلك يعني أن
   النص لم يطابق منتجًا في المتجر، لا أن العميل سأل عن منتج غير موجود؛ فاستوضح بدل
   أن تجزم بعدم وجوده.
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
