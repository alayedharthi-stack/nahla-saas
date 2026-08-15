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
TOPIC_STORE_ABOUT = "store_about"
TOPIC_STORE_SOCIAL = "store_social"
TOPIC_STORE_CURRENCY = "store_currency"
TOPIC_STORE_STATUS = "store_status"
# Physical-shop / Google-Maps location topic. Routed here from
# INTENT_ASK_LOCATION (May 2026 #36) so the deterministic ``faq_location``
# template can ship ``maps_url`` instead of falling through to
# ``store_url`` like the older single-topic path used to.
TOPIC_LOCATION      = "location"
TOPIC_OWNER_CONTACT = "owner_contact"


class FAQReplyHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        topic = str(decision.args.get("topic") or "").strip()
        social = getattr(ctx.facts, "merchant_profile_social_links", None) or {}
        if not isinstance(social, dict):
            social = {}
        payload = {
            "store_name": ctx.facts.store_name,
            "store_url": str(getattr(ctx.facts, "store_url", "") or "").strip(),
            # Maps URL surfaced for the location FAQ template; empty
            # string when the merchant has not configured a maps link
            # in any source. The template's no-URL path is honest
            # ("share more about which branch you're after") instead
            # of silently substituting the e-commerce store_url.
            "maps_url": ctx.facts.maps_url,
            "store_description": ctx.facts.store_description,
            # Pack A2: public profile phone only (never WA owner number).
            "contact_phone": ctx.facts.store_contact_phone,
            "contact_email": ctx.facts.store_contact_email,
            "social_links": social,
            "currency": getattr(ctx.facts, "merchant_profile_currency", "") or "",
            "store_status": getattr(ctx.facts, "merchant_profile_status", "") or "",
            "shipping_methods": ctx.facts.shipping_methods,
            "shipping_notes": ctx.facts.shipping_notes,
            "shipping_policy": ctx.facts.shipping_policy,
            "support_hours": ctx.facts.support_hours,
            "payment_methods": ctx.facts.payment_methods,
        }
        if not payload["store_url"]:
            try:
                from modules.ai.brain.commerce.store_inquiry_compose_guard import (  # noqa: PLC0415
                    apply_store_url_to_facts,
                )

                apply_store_url_to_facts(
                    ctx.facts,
                    getattr(ctx, "_db", None),
                    int(getattr(ctx, "tenant_id", 0) or 0),
                )
                payload["store_url"] = str(getattr(ctx.facts, "store_url", "") or "").strip()
            except Exception:  # noqa: BLE001  # noqa: silent-ok — resolver must not block FAQ
                pass
        return ActionResult(
            success=bool(topic),
            data={
                "type": "faq",
                "topic": topic,
                "payload": payload,
            },
            error=None if topic else "missing_faq_topic",
        )
