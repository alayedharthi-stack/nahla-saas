"""
template_filter_metadata.py
───────────────────────────
Capability and channel metadata for Nahla library templates.

Read-path only — does not alter Meta template bodies. Merchants may still
customize imported template text via the dashboard editor (``customizable``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

OrderChannel = str  # external_store | whatsapp | any

# Slot names → capability fields required when present in template body/buttons.
_SLOT_CAPABILITY_REQUIREMENTS: Dict[str, Tuple[str, ...]] = {
    "checkout_url":        ("supports_external_checkout",),
    "cart_url":            ("supports_external_checkout",),
    "store_url":           ("supports_external_checkout",),
    "store_or_product_url": ("supports_external_checkout",),
    "product_url":         ("supports_external_checkout",),
    "reorder_url":         ("supports_external_checkout",),
    "payment_url":         ("has_payment_link",),
    "discount_code":       ("supports_external_coupons",),
    "vip_coupon":          ("supports_external_coupons",),
    "coupon_code":         ("supports_external_coupons",),
}

_EXTERNAL_TRACKING_SLOTS = frozenset({"tracking_url", "order_tracking_url"})


@dataclass(frozen=True)
class TemplateFilterMeta:
    intent: str
    order_channel: OrderChannel
    required_capabilities: Tuple[str, ...] = ()
    required_variables: Tuple[str, ...] = ()
    required_buttons: Tuple[str, ...] = ()
    customizable: bool = True


# Explicit overrides — keys must match ``NAHLA_TEMPLATES[].key``.
_TEMPLATE_OVERRIDES: Dict[str, TemplateFilterMeta] = {
    # ── External store / recovery ─────────────────────────────────────────
    "abandoned_cart_reminder": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="external_store",
        required_variables=("customer_name",),
        required_buttons=("checkout_url",),
    ),
    "complete_your_order": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="external_store",
        required_buttons=("checkout_url",),
    ),
    "comeback_discount": TemplateFilterMeta(
        intent="customer_winback",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "abandoned_cart_24h_coupon": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
        required_buttons=("checkout_url",),
    ),
    "abandoned_cart_3day_final": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="external_store",
        required_buttons=("checkout_url",),
    ),
    "special_offer": TemplateFilterMeta(
        intent="marketing_campaign",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "marketing_campaign": TemplateFilterMeta(
        intent="marketing_campaign",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "seasonal_offer_template": TemplateFilterMeta(
        intent="seasonal_offer",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "salary_payday_offer_template": TemplateFilterMeta(
        intent="salary_payday_offer",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "vip_exclusive": TemplateFilterMeta(
        intent="vip_reward",
        order_channel="external_store",
        required_capabilities=("supports_external_coupons",),
    ),
    "new_arrivals": TemplateFilterMeta(
        intent="product_announcement",
        order_channel="external_store",
        required_variables=("product_url",),
    ),
    "back_in_stock_alert": TemplateFilterMeta(
        intent="back_in_stock",
        order_channel="external_store",
        required_variables=("product_url",),
    ),
    "predictive_reorder_reminder": TemplateFilterMeta(
        intent="predictive_reorder",
        order_channel="external_store",
        required_variables=("reorder_url",),
    ),
    "reorder_quick_link": TemplateFilterMeta(
        intent="predictive_reorder",
        order_channel="external_store",
        required_variables=("reorder_url",),
    ),
    "payment_reminder": TemplateFilterMeta(
        intent="payment_pending",
        order_channel="external_store",
        required_buttons=("payment_url",),
    ),
    "interested_followup": TemplateFilterMeta(
        intent="customer_engagement",
        order_channel="external_store",
        required_variables=("product_url",),
    ),
    # ── WhatsApp / Nahla orders ───────────────────────────────────────────
    "wa_abandoned_order_draft": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="whatsapp",
        required_capabilities=("supports_nahla_orders",),
    ),
    # ── Shared / channel-agnostic (capability-gated) ──────────────────────
    "post_purchase_thanks": TemplateFilterMeta(
        intent="order_confirmed",
        order_channel="any",
        required_buttons=("tracking_url",),
    ),
    "order_summary": TemplateFilterMeta(
        intent="order_confirmed",
        order_channel="any",
        required_buttons=("tracking_url",),
    ),
    "order_confirmed": TemplateFilterMeta(
        intent="order_confirmed",
        order_channel="any",
    ),
    "shipping_update": TemplateFilterMeta(
        intent="order_shipped",
        order_channel="any",
        required_buttons=("tracking_url",),
    ),
    "order_out_for_delivery": TemplateFilterMeta(
        intent="out_for_delivery",
        order_channel="any",
        required_buttons=("tracking_url",),
    ),
    "order_delivered": TemplateFilterMeta(
        intent="order_delivered",
        order_channel="any",
    ),
    "review_request": TemplateFilterMeta(
        intent="review_request",
        order_channel="any",
    ),
    "cod_confirmation": TemplateFilterMeta(
        intent="cod_confirmation",
        order_channel="any",
        required_capabilities=("supports_cod",),
    ),
    "cod_reminder_before_shipping": TemplateFilterMeta(
        intent="cod_confirmation",
        order_channel="any",
        required_capabilities=("supports_cod",),
    ),
    "welcome_message": TemplateFilterMeta(
        intent="welcome",
        order_channel="any",
    ),
    "support_followup": TemplateFilterMeta(
        intent="customer_support",
        order_channel="any",
    ),
    # Meta review helpers — visibility only when merchant has matching channel
    "meta_review_cart_recovery": TemplateFilterMeta(
        intent="incomplete_order",
        order_channel="external_store",
        required_buttons=("checkout_url",),
    ),
    "meta_review_order_confirmation": TemplateFilterMeta(
        intent="order_confirmed",
        order_channel="any",
    ),
    "meta_review_delivery_update": TemplateFilterMeta(
        intent="order_shipped",
        order_channel="any",
        required_buttons=("tracking_url",),
    ),
}


def _infer_intent(tpl: Dict[str, Any]) -> str:
    trigger = str(tpl.get("smart_trigger") or "").strip()
    if trigger:
        return trigger
    service = str(tpl.get("service_key") or "").strip()
    if service:
        return service
    return str(tpl.get("key") or "general")


def _infer_order_channel(tpl: Dict[str, Any]) -> OrderChannel:
    tags = tpl.get("filter_tags") or []
    service = str(tpl.get("service_key") or "")
    if "recovery" in tags and service in {"cart_recovery", ""}:
        return "external_store"
    if service == "wa_draft_reminder":
        return "whatsapp"
    return "any"


def resolve_template_filter_meta(tpl: Dict[str, Any]) -> TemplateFilterMeta:
    """Return filter metadata for a library template dict."""
    key = str(tpl.get("key") or "")
    if key in _TEMPLATE_OVERRIDES:
        return _TEMPLATE_OVERRIDES[key]

    slots = tuple(str(s) for s in (tpl.get("slots") or []))
    body_slots = tuple(str(s) for s in (tpl.get("body_slots") or slots))
    button_slots = tuple(str(s) for s in (tpl.get("button_slots") or ()))

    # Infer button slots from URL buttons when not declared
    if not button_slots:
        for comp in tpl.get("components") or []:
            if comp.get("type") != "BUTTONS":
                continue
            for btn in comp.get("buttons") or []:
                url = str(btn.get("url") or "")
                if "{{" in url:
                    if "checkout" in url or "cart" in url:
                        button_slots = button_slots + ("checkout_url",)
                    elif "track" in url:
                        button_slots = button_slots + ("tracking_url",)
                    elif "pay" in url:
                        button_slots = button_slots + ("payment_url",)

    required_caps: List[str] = []
    for slot in set(body_slots) | set(button_slots) | set(slots):
        required_caps.extend(_SLOT_CAPABILITY_REQUIREMENTS.get(slot, ()))

    return TemplateFilterMeta(
        intent=_infer_intent(tpl),
        order_channel=_infer_order_channel(tpl),
        required_capabilities=tuple(dict.fromkeys(required_caps)),
        required_variables=body_slots,
        required_buttons=button_slots,
        customizable=True,
    )


def tracking_capability_for_channel(
    order_channel: OrderChannel,
    caps: Any,
) -> bool:
    """True when a tracking-button template may be shown for this channel."""
    if order_channel == "whatsapp":
        return bool(getattr(caps, "has_nahla_tracking", False))
    if order_channel == "external_store":
        return bool(getattr(caps, "has_external_tracking", False))
    # any — require at least one real tracking source
    return bool(
        getattr(caps, "has_external_tracking", False)
        or getattr(caps, "has_nahla_tracking", False)
    )


def template_passes_capabilities(
    meta: TemplateFilterMeta,
    caps: Any,
) -> bool:
    """Return False when merchant lacks a required capability or URL evidence."""
    for req in meta.required_capabilities:
        if not bool(getattr(caps, req, False)):
            return False

    all_slots = set(meta.required_variables) | set(meta.required_buttons)
    for slot in all_slots:
        for req in _SLOT_CAPABILITY_REQUIREMENTS.get(slot, ()):
            if not bool(getattr(caps, req, False)):
                return False

    tracking_slots = all_slots & _EXTERNAL_TRACKING_SLOTS
    if tracking_slots:
        if not tracking_capability_for_channel(meta.order_channel, caps):
            return False

    return True


def filter_meta_to_dict(meta: TemplateFilterMeta) -> Dict[str, Any]:
    return {
        "intent":                meta.intent,
        "order_channel":         meta.order_channel,
        "required_capabilities": list(meta.required_capabilities),
        "required_variables":    list(meta.required_variables),
        "required_buttons":      list(meta.required_buttons),
        "customizable":          meta.customizable,
    }


__all__ = [
    "TemplateFilterMeta",
    "filter_meta_to_dict",
    "resolve_template_filter_meta",
    "template_passes_capabilities",
    "tracking_capability_for_channel",
]
