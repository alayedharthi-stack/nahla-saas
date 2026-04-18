"""
brain/execution/faq.py
──────────────────────
Deterministic FAQ handler for simple MerchantBrain questions that do not need
LLM reasoning, such as store identity, shipping basics, and contact details.
"""
from __future__ import annotations

from ..types import ActionResult, BrainContext, Decision


TOPIC_IDENTITY = "identity"
TOPIC_SHIPPING = "shipping"
TOPIC_STORE_INFO = "store_info"
TOPIC_OWNER_CONTACT = "owner_contact"


class FAQReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        topic = str(decision.args.get("topic") or "").strip()
        payload = {
            "store_name": ctx.facts.store_name,
            "store_url": ctx.facts.store_url,
            "store_description": ctx.facts.store_description,
            "contact_phone": ctx.facts.store_contact_phone,
            "contact_email": ctx.facts.store_contact_email,
            "shipping_methods": ctx.facts.shipping_methods,
            "shipping_notes": ctx.facts.shipping_notes,
            "shipping_policy": ctx.facts.shipping_policy,
            "support_hours": ctx.facts.support_hours,
            "payment_methods": ctx.facts.payment_methods,
        }
        return ActionResult(
            success=bool(topic),
            data={
                "type": "faq",
                "topic": topic,
                "payload": payload,
            },
            error=None if topic else "missing_faq_topic",
        )
