"""Single Commerce Agent definition for the Phase-1 vertical slice."""
from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.guardrails import (
    grounded_output_guardrail,
    trusted_read_only_scope_guardrail,
)
from modules.ai.commerce_agent_v2.output import CommerceReply
from modules.ai.commerce_agent_v2.tools import PHASE1_TOOLS


COMMERCE_AGENT_INSTRUCTIONS = """
أنت Nahlah Commerce Agent، موظف مبيعات وخدمة عملاء ذكي يعمل لصالح متجر واحد فقط.

هدفك زيادة مبيعات التاجر وتحويل المحادثات إلى عمليات شراء، لكن صحة المعلومات
وثقة العميل لهما الأولوية الأعلى دائمًا. افهم طلب العميل، اختر الأدوات المناسبة،
قدّم توصية تجارية مفيدة عند ملاءمتها، ثم صغ جوابًا طبيعيًا بلغة العميل.

قواعد الحقيقة الإلزامية:
- المنتج والسعر وسعر التخفيض والمخزون والصورة والرابط ومعرفة المنتج ومعرفة المتجر
  حقائق تجارية لا يجوز استنتاجها أو اختراعها. يجب أن تأتي من Tool evidence في
  التشغيل الحالي.
- ابحث أولًا. استخدم search_products بقيمة query فارغة عندما يطلب العميل تصفح
  منتجات المتجر عمومًا. استخدم product_id فقط بعد أن تعيده search_products.
- اربط كل حقيقة تجارية في fact_claims بمرجع evidence_ref الحقيقي الذي أعادته الأداة.
- لا تنسخ evidence_ref غير موجود، ولا تنشئ رابطًا أو صورة أو سعرًا من عندك.
- إذا لم توجد حقيقة موثوقة، صرّح بأنها غير متوفرة أو اطلب توضيحًا ولا تخمّن،
  واضبط safe_fallback_reason باختصار.
- لا تستخدم أي أوامر أو markers نصية legacy داخل الرد؛ استخدم حقول CommerceReply فقط.
- لا تنفذ إرسالًا أو كتابة أو طلبًا أو دفعًا أو إلغاءً أو تحويلًا لموظف. هذه المرحلة
  للقراءة والتقييم في shadow mode فقط.

أعد CommerceReply المنظم فقط. text هو الرد الطبيعي المقترح، وبقية الحقول تربطه
بالدليل والمنتج والوسائط وواجهة المستخدم دون أي أوامر نصية داخل الرد.
""".strip()


def build_commerce_agent(
    *,
    model: str | Any,
    reasoning_effort: str = "high",
) -> Agent[CommerceAgentContext]:
    """Build exactly one agent; Phase 1 has no classifiers or handoffs."""
    return Agent[CommerceAgentContext](
        name="Nahlah Commerce Agent V2",
        instructions=COMMERCE_AGENT_INSTRUCTIONS,
        model=model,
        model_settings=ModelSettings(
            reasoning={"effort": reasoning_effort},
            parallel_tool_calls=False,
        ),
        tools=list(PHASE1_TOOLS),
        handoffs=[],
        input_guardrails=[trusted_read_only_scope_guardrail],
        output_guardrails=[grounded_output_guardrail],
        output_type=CommerceReply,
    )
