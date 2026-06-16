"""
core/template_library.py
─────────────────────────
Single source of truth for Nahla's **default automation templates**.

Why this file exists
────────────────────
The product requirement is: "a merchant connects WhatsApp and these
automations start working immediately, with pre-approved templates and safe
variables — merchants can edit text but cannot remove required slots."

Two distinct concerns meet here:

  1. The Meta WhatsApp Cloud API only supports numeric placeholders
     (`{{1}}`, `{{2}}`, …). The numeric form is what gets submitted and
     approved. We *cannot* ship `{{customer_name}}` style placeholders to
     Meta — they would be treated as literal text.

  2. The merchant-facing dashboard wants named slots like
     `{{customer_name}}`, `{{checkout_url}}`, `{{vip_coupon}}`. These are
     friendlier to read and harder to break.

So we keep both: the canonical body uses Meta's `{{1}}/{{2}}/…`, and a
fixed `var_map` records *which named slot each numeric placeholder
represents*. The placeholder integrity validator (in routers/templates.py)
already prevents merchants from adding/removing/re-ordering placeholders,
so the named contract stays intact even when merchants reword the body.

Anything outside this library is merchant-authored and not policed here.
"""
from __future__ import annotations

from typing import Any, Dict, List


# ── Named variable slots (the "safe variables" the product spec defines) ──
#
# These are the ONLY variable names the dashboard and AI assistant are
# allowed to expose. Adding a new slot requires (a) updating this set and
# (b) teaching `automation_engine._build_template_vars` how to resolve it.
ALLOWED_VARIABLE_SLOTS: frozenset[str] = frozenset({
    "customer_name",
    "store_name",
    "cart_total",
    "checkout_url",
    "discount_code",
    "vip_coupon",
    "store_url",
    "product_name",
    "product_url",
    "order_id",
    "payment_url",     # unpaid_order_reminder
    "reorder_url",     # predictive_reorder_reminder
    "occasion_name",   # seasonal_offer (e.g. "اليوم الوطني")
    # manual_coupon_campaign — Salla coupon API hasn't approved us yet, so the
    # merchant types the coupon code + offer copy + URL by hand and we just
    # ship it. None of these are auto-resolved by the engine; they ALL come
    # from the merchant's form on the dashboard.
    "merchant_name",
    "offer_description",
    "coupon_code",
    "discount_text",
    "expiry_text",
    "store_or_product_url",
})


# ── Template categories valid under WhatsApp Business policy ──────────────
ALLOWED_CATEGORIES: frozenset[str] = frozenset({"MARKETING", "UTILITY"})


# ── Default template specs ────────────────────────────────────────────────
#
# Each entry is one *automation feature* and carries one body per language.
# The dict keys are the canonical template names that will be inserted into
# `whatsapp_templates.name` and submitted to Meta. The numeric form
# (`{{1}}`, `{{2}}`) is what actually ships; the `slots` list documents
# which named slot each numeric placeholder represents and is the contract
# the engine uses when resolving variables at send-time.
#
# Structure:
#   <feature_key>: {
#     "automation_type":  matches SmartAutomation.automation_type
#     "trigger_event":    canonical AutomationTrigger value
#     "category":         MARKETING | UTILITY
#     "languages": {
#         "ar"|"en": {
#             "template_name": Meta-safe snake_case unique name
#             "components":    Meta WhatsApp components payload
#             "slots":         ordered list of named variables → maps to
#                              {{1}}, {{2}}, … positionally
#         }
#     }
#   }
DEFAULT_AUTOMATION_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # ── 1) Abandoned cart recovery ───────────────────────────────────────
    "cart_abandoned": {
        "automation_type": "abandoned_cart",
        "trigger_event":   "cart_abandoned",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "abandoned_cart_recovery_ar",
                "body_slots":   ["customer_name"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 🛒\n\n"
                            "لاحظنا أنك أضفت بعض المنتجات إلى سلتك لكن لم تكتمل عملية الشراء.\n\n"
                            "سلتك ما زالت محفوظة وتنتظرك — أكمل طلبك الآن قبل نفاد المخزون!"
                        ),
                        "example": {"body_text": [["أحمد"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "أكمل طلبك",
                            "url": "https://example.com/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
            "en": {
                "template_name": "abandoned_cart_recovery_en",
                "body_slots":   ["customer_name"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 🛒\n\n"
                            "We noticed you added some items to your cart but didn't complete checkout.\n\n"
                            "Your cart is still saved and waiting — finish checkout before items go out of stock!"
                        ),
                        "example": {"body_text": [["Ahmad"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "Complete order",
                            "url": "https://example.com/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
        },
    },

    # ── 2) Inactive customer win-back ────────────────────────────────────
    "customer_inactive": {
        "automation_type": "customer_winback",
        "trigger_event":   "customer_inactive",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "win_back_ar",
                "slots":         ["customer_name", "store_name", "discount_code"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 🌟\n\n"
                            "اشتقنا لك في متجر {{2}}!\n\n"
                            "يسعدنا أن نقدم لك عرضاً خاصاً للعودة للتسوق معنا.\n\n"
                            "استخدم الكود التالي عند الشراء:\n\n"
                            "{{3}}\n\n"
                            "نتطلع لخدمتك مجدداً 💛"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                ],
            },
            "en": {
                "template_name": "win_back_en",
                "slots":         ["customer_name", "store_name", "discount_code"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 🌟\n\n"
                            "We've missed you at {{2}}!\n\n"
                            "Here's a small thank-you to welcome you back — use this code at checkout:\n\n"
                            "{{3}}\n\n"
                            "Looking forward to serving you again 💛"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                ],
            },
        },
    },

    # ── 4) Product back in stock ────────────────────────────────────────
    #
    # Fan-out template. Triggered when a product transitions from
    # stock_quantity=0 → >0. The engine fans out one execution per pending
    # ProductInterest row for the restocked product. Category MARKETING
    # because Meta routes "the thing you wanted is now available" as a
    # promotional nudge, not a transactional update.
    "product_back_in_stock": {
        "automation_type": "back_in_stock",
        "trigger_event":   "product_back_in_stock",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "back_in_stock_ar",
                "slots":         ["customer_name", "store_name", "product_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "المنتج الذي كنت تنتظره عاد للمخزون في متجر {{2}} 🎉\n\n"
                            "يمكنك الطلب الآن من هنا:\n\n"
                            "{{3}}\n\n"
                            "نسعد بخدمتك دائماً 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "اطلب الآن", "url": "{{3}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "back_in_stock_en",
                "slots":         ["customer_name", "store_name", "product_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "The product you were waiting for is back in stock at {{2}} 🎉\n\n"
                            "You can order it now from here:\n\n"
                            "{{3}}\n\n"
                            "Always happy to serve you 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Order now", "url": "{{3}}"}],
                    },
                ],
            },
        },
    },

    # ── 5b) WhatsApp abandoned draft order reminder (PR-5) ───────────────
    "abandoned_order_draft": {
        "automation_type": "abandoned_order_draft",
        "trigger_event":   "wa_order_draft_reminder_due",
        "category":        "UTILITY",
        "languages": {
            "ar": {
                "template_name": "wa_abandoned_order_draft_ar",
                "body_slots":    ["customer_name", "store_name"],
                "slots":         ["customer_name", "store_name"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "لديك طلب مبدئي في {{2}} لم يكتمل بعد.\n"
                            "أكمل الطلب من المحادثة وسنساعدك خطوة بخطوة 🌟"
                        ),
                        "example": {"body_text": [["أحمد", "متجر الأناقة"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                ],
            },
        },
    },

    # ── 5) Unpaid order reminder (recovery engine) ──────────────────────
    #
    # Triggered by `automation_emitters.scan_unpaid_orders` for orders that
    # have been left in `pending` / `awaiting_payment` past the configured
    # grace window. Three escalating steps; the engine picks the right one
    # by event age. The CTA is a button to the order's payment URL when the
    # store integration provides one (Salla/Zid/Shopify all do).
    "unpaid_order_reminder": {
        "automation_type": "unpaid_order_reminder",
        "trigger_event":   "order_payment_pending",
        "category":        "UTILITY",
        "languages": {
            "ar": {
                "template_name": "unpaid_order_reminder_ar",
                # body_slots only — payment_url goes to the URL button ({{1}} suffix),
                # NOT as a {{4}} body variable. Meta requires the button parameter
                # namespace to be independent from the body (always {{1}}).
                "body_slots":    ["customer_name", "order_id", "store_name"],
                "slots":         ["customer_name", "order_id", "store_name"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "طلبك رقم #{{2}} في متجر {{3}} لا يزال بانتظار الدفع.\n\n"
                            "أكمل الدفع الآن من هنا وضمّن طلبك قبل نفاذ المخزون:\n\n"
                            "إذا واجهت أي مشكلة في الدفع نحن هنا لمساعدتك 🌟"
                        ),
                        "example": {"body_text": [["أحمد", "78901", "متجر الأناقة"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "URL",
                                "text": "إكمال الدفع",
                                "url":  "https://store.example.com/pay/{{1}}",
                                "example": ["https://store.example.com/pay/order-78901"],
                            },
                        ],
                    },
                ],
            },
            "en": {
                "template_name": "unpaid_order_reminder_en",
                "body_slots":    ["customer_name", "order_id", "store_name"],
                "slots":         ["customer_name", "order_id", "store_name"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "Your order #{{2}} at {{3}} is still awaiting payment.\n\n"
                            "Complete your payment now to secure your order:\n\n"
                            "Reply if you ran into any trouble during checkout 🌟"
                        ),
                        "example": {"body_text": [["Ahmed", "78901", "My Store"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "URL",
                                "text": "Complete payment",
                                "url":  "https://store.example.com/pay/{{1}}",
                                "example": ["https://store.example.com/pay/order-78901"],
                            },
                        ],
                    },
                ],
            },
        },
    },

    # ── 6) Predictive reorder reminder (growth engine) ──────────────────
    #
    # Triggered by `automation_emitters.scan_predictive_reorders` a few days
    # before a customer's next predicted reorder date for a consumable
    # product. UTILITY category because we are reminding about a likely
    # need rather than promoting.
    "predictive_reorder": {
        "automation_type": "predictive_reorder",
        "trigger_event":   "predictive_reorder_due",
        "category":        "UTILITY",
        "languages": {
            "ar": {
                "template_name": "predictive_reorder_reminder_ar",
                "slots":         ["customer_name", "product_name", "reorder_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "نتوقع أنك على وشك الحاجة لإعادة طلب {{2}}.\n\n"
                            "يمكنك إعادة الطلب بسرعة من هنا:\n\n"
                            "{{3}}\n\n"
                            "نسعد بخدمتك دائماً 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "إعادة الطلب", "url": "{{3}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "predictive_reorder_reminder_en",
                "slots":         ["customer_name", "product_name", "reorder_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "You're probably running low on {{2}} — want a quick top-up?\n\n"
                            "Reorder in one tap:\n\n"
                            "{{3}}\n\n"
                            "Always happy to serve you 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Reorder", "url": "{{3}}"}],
                    },
                ],
            },
        },
    },

    # ── 7) Seasonal offer (growth engine) ───────────────────────────────
    #
    # Triggered one day before each entry in the built-in Saudi calendar
    # (national_day, founding_day, ramadan, eid_fitr, eid_adha, white_friday).
    # The occasion name is injected by the calendar emitter; the discount
    # code comes from the auto_coupon pool when enabled.
    "seasonal_offer": {
        "automation_type": "seasonal_offer",
        "trigger_event":   "seasonal_event_due",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "seasonal_offer_ar",
                "slots":         ["customer_name", "occasion_name", "store_name", "discount_code", "store_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 🌟\n\n"
                            "بمناسبة {{2}}، نقدم لك عرضاً خاصاً في متجر {{3}}.\n\n"
                            "استخدم الكود التالي عند الشراء:\n\n"
                            "{{4}}\n\n"
                            "تسوق الآن:\n\n"
                            "{{5}}"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "تسوق الآن", "url": "{{5}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "seasonal_offer_en",
                "slots":         ["customer_name", "occasion_name", "store_name", "discount_code", "store_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 🌟\n\n"
                            "To celebrate {{2}}, we have a special offer for you at {{3}}.\n\n"
                            "Use this code at checkout:\n\n"
                            "{{4}}\n\n"
                            "Shop now:\n\n"
                            "{{5}}"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Shop now", "url": "{{5}}"}],
                    },
                ],
            },
        },
    },

    # ── 8) Salary payday offer (growth engine) ──────────────────────────
    #
    # Triggered one day before each tenant's configured payday (default 27th
    # of the Gregorian month — Saudi private sector). Same shape as the
    # generic seasonal offer minus the occasion slot, with copy tuned for
    # post-salary spending behaviour.
    "salary_payday_offer": {
        "automation_type": "salary_payday_offer",
        "trigger_event":   "salary_payday_due",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "salary_payday_offer_ar",
                "slots":         ["customer_name", "store_name", "discount_code", "store_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 🌟\n\n"
                            "بمناسبة قرب موعد الراتب، أعد متجر {{2}} عرضاً خاصاً لك.\n\n"
                            "استخدم الكود التالي قبل انتهاء العرض:\n\n"
                            "{{3}}\n\n"
                            "تسوق الآن:\n\n"
                            "{{4}}"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "تسوق الآن", "url": "{{4}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "salary_payday_offer_en",
                "slots":         ["customer_name", "store_name", "discount_code", "store_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 🌟\n\n"
                            "Payday's around the corner — {{2}} put together a little something for you.\n\n"
                            "Use this code at checkout:\n\n"
                            "{{3}}\n\n"
                            "Shop now:\n\n"
                            "{{4}}"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Shop now", "url": "{{4}}"}],
                    },
                ],
            },
        },
    },

    # ── 9) Abandoned cart — stage 2 (6h follow-up, no discount) ─────────
    #
    # Sent by `automation_emitters.scan_abandoned_cart_followups` six
    # hours after the original abandonment if the cart is still open.
    # Tone: empathetic, "do you need help?" — explicitly NO coupon at
    # this stage. Same slot contract as stage 1 so the same store/url
    # vars resolve uniformly.
    "abandoned_cart_followup": {
        "automation_type": "abandoned_cart",
        "trigger_event":   "cart_abandoned",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "abandoned_cart_followup_ar",
                # Same body-vs-button split as stage 1 — keep the BODY
                # tight (just the customer name) and let the URL button
                # carry the cart link via the engine's URL-slot resolver.
                "body_slots":   ["customer_name"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 🌷\n\n"
                            "ما زال طلبك بانتظارك.\n\n"
                            "إذا واجهتَ أي صعوبة في إتمام الطلب — سواء بالدفع أو "
                            "الشحن أو معلومات المنتج — يسعدنا مساعدتك مباشرة عبر هذه المحادثة.\n\n"
                            "أو يمكنك إكمال الطلب من هنا متى أردت:"
                        ),
                        "example": {"body_text": [["أحمد"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "إكمال الطلب",
                            "url": "https://example.com/checkout/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
            "en": {
                "template_name": "abandoned_cart_followup_en",
                "body_slots":   ["customer_name"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 🌷\n\n"
                            "Your cart is still waiting for you.\n\n"
                            "If you ran into any trouble — payment, shipping, or "
                            "questions about the product — just reply here and we'll help.\n\n"
                            "Or pick up where you left off any time:"
                        ),
                        "example": {"body_text": [["Ahmad"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "Complete order",
                            "url": "https://example.com/checkout/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
        },
    },

    # ── 10) Abandoned cart — stage 3 (24h, optional coupon) ────────────
    #
    # Last reminder. The merchant's `auto_coupon=True` step config (or
    # the OfferDecisionService when the tenant is on ENFORCE/ADVISORY)
    # decides whether `discount_code` is populated. When the resolver
    # returns no code, the engine renders an empty discount slot — the
    # template still ships, just without the price-off line.
    "abandoned_cart_final_offer": {
        "automation_type": "abandoned_cart",
        "trigger_event":   "cart_abandoned",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "abandoned_cart_final_offer_ar",
                # Stage-3 body keeps customer name + discount code only.
                # The URL button is fed independently from checkout_url —
                # never share a {{N}} index between body and button.
                "body_slots":   ["customer_name", "discount_code"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "discount_code", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 💛\n\n"
                            "آخر تذكير لطلبك المحفوظ.\n\n"
                            "حضّرنا لك عرضاً صغيراً لتجربة مريحة — استخدم الكود التالي:\n\n"
                            "{{2}}\n\n"
                            "ثم أكمل طلبك من الزر بالأسفل."
                        ),
                        "example": {"body_text": [["أحمد", "WELCOME10"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "إكمال الطلب",
                            "url": "https://example.com/checkout/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
            "en": {
                "template_name": "abandoned_cart_final_offer_en",
                "body_slots":   ["customer_name", "discount_code"],
                "button_slots": ["checkout_url"],
                "slots":         ["customer_name", "discount_code", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 💛\n\n"
                            "Last nudge for your saved cart.\n\n"
                            "We've prepared a small offer to make this easy — use this code:\n\n"
                            "{{2}}\n\n"
                            "Then tap the button below to finish checkout."
                        ),
                        "example": {"body_text": [["Ahmad", "WELCOME10"]]},
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL", "text": "Complete order",
                            "url": "https://example.com/checkout/{{1}}",
                            "example": ["https://example.com/checkout/abc123"],
                        }],
                    },
                ],
            },
        },
    },

    # ── 11) COD confirmation reminder ──────────────────────────────────
    #
    # Sent by `automation_emitters.scan_cod_confirmations` when a COD
    # order has been in `pending_confirmation` for the configured
    # window (default 6 h) without the customer tapping the QUICK_REPLY
    # buttons on the original `cod_order_confirmation_ar` template.
    #
    # UTILITY category because this is a confirmation request for a
    # transactional flow the customer themselves initiated, not a
    # promotional nudge.
    "cod_confirmation_reminder": {
        "automation_type": "cod_confirmation",
        "trigger_event":   "order_cod_pending",
        "category":        "UTILITY",
        "languages": {
            "ar": {
                "template_name": "cod_confirmation_reminder_ar",
                "slots":         ["customer_name", "order_id", "store_name"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "طلبك رقم #{{2}} في متجر {{3}} بانتظار تأكيدك.\n\n"
                            "للتأكيد فقط ردّ على هذه الرسالة بكلمة \"تأكيد\"، "
                            "وللإلغاء ردّ بكلمة \"إلغاء\".\n\n"
                            "إن لم نتلقَّ ردّك خلال الفترة المحددة سيتم إلغاء الطلب تلقائياً 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                ],
            },
            "en": {
                "template_name": "cod_confirmation_reminder_en",
                "slots":         ["customer_name", "order_id", "store_name"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "Your order #{{2}} at {{3}} is still waiting for your confirmation.\n\n"
                            "Reply \"confirm\" to confirm, or \"cancel\" to cancel.\n\n"
                            "If we don't hear back, the order will be cancelled automatically 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                ],
            },
        },
    },

    # ── 12) Manual coupon campaign (interim — pre Salla coupon API) ────
    #
    # Manual fallback for merchants who want to push a discount over WhatsApp
    # while we wait for Salla's coupon-creation API approval. The merchant
    # creates the coupon inside Salla themselves, then types the code + the
    # storefront URL into the dashboard. Nahla simply renders an APPROVED
    # template + URL button; nothing is auto-generated, no code is validated
    # against Salla, and the engine never picks this template up for any
    # automation flow (`automation_type: "manual_only"` is unique and not
    # subscribed by any emitter).
    "manual_coupon_campaign": {
        "automation_type": "manual_only",
        "trigger_event":   "manual_only",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "manual_coupon_campaign_ar",
                # Body carries everything the merchant types — six numbered
                # placeholders, NO auto-resolved slots. The URL button uses
                # its own independent ``{{1}}`` (Meta-compliant button
                # parameter), populated at send-time from the merchant's
                # ``store_or_product_url`` via the standard URL-button suffix
                # extractor (test_send_urls.extract_button_suffix).
                "body_slots": [
                    "customer_name",
                    "merchant_name",
                    "offer_description",
                    "coupon_code",
                    "discount_text",
                    "expiry_text",
                ],
                "button_slots": ["store_or_product_url"],
                "slots": [
                    "customer_name",
                    "merchant_name",
                    "offer_description",
                    "coupon_code",
                    "discount_text",
                    "expiry_text",
                    "store_or_product_url",
                ],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "عرض خاص من {{2}} 🍯\n\n"
                            "{{3}}\n\n"
                            "استخدم كود الخصم:\n"
                            "{{4}}\n\n"
                            "الخصم:\n"
                            "{{5}}\n\n"
                            "{{6}}\n\n"
                            "اضغط الزر بالأسفل للطلب من المتجر."
                        ),
                        "example": {
                            "body_text": [[
                                "أحمد",
                                "متجر نحلة",
                                "عرض اليوم على المنتجات المختارة فقط",
                                "NAHLA10",
                                "خصم 10%",
                                "العرض ساري حتى نهاية الأسبوع",
                            ]],
                        },
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL",
                            "text": "اطلب الآن",
                            # Meta requires a base URL with a {{1}} suffix —
                            # the merchant submits a full URL on the dashboard
                            # and the test-send pipeline strips the prefix
                            # and forwards the path as the {{1}} parameter
                            # (see ``test_send_urls.extract_button_suffix``).
                            "url": "https://example.com/{{1}}",
                            "example": ["https://example.com/products/special"],
                        }],
                    },
                ],
            },
            "en": {
                "template_name": "manual_coupon_campaign_en",
                "body_slots": [
                    "customer_name",
                    "merchant_name",
                    "offer_description",
                    "coupon_code",
                    "discount_text",
                    "expiry_text",
                ],
                "button_slots": ["store_or_product_url"],
                "slots": [
                    "customer_name",
                    "merchant_name",
                    "offer_description",
                    "coupon_code",
                    "discount_text",
                    "expiry_text",
                    "store_or_product_url",
                ],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "A special offer from {{2}} 🍯\n\n"
                            "{{3}}\n\n"
                            "Use this coupon code:\n"
                            "{{4}}\n\n"
                            "Discount:\n"
                            "{{5}}\n\n"
                            "{{6}}\n\n"
                            "Tap the button below to shop now."
                        ),
                        "example": {
                            "body_text": [[
                                "Ahmad",
                                "Nahla Store",
                                "Today's special on selected items",
                                "NAHLA10",
                                "10% off",
                                "Offer valid until the end of the week",
                            ]],
                        },
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{
                            "type": "URL",
                            "text": "Shop now",
                            "url": "https://example.com/{{1}}",
                            "example": ["https://example.com/products/special"],
                        }],
                    },
                ],
            },
        },
    },

    # ── 3) VIP customer reward ───────────────────────────────────────────
    "vip_customer_upgrade": {
        "automation_type": "vip_upgrade",
        "trigger_event":   "vip_customer_upgrade",
        "category":        "MARKETING",
        "languages": {
            "ar": {
                "template_name": "vip_reward_ar",
                "slots":         ["customer_name", "store_name", "vip_coupon"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👑\n\n"
                            "شكراً لك لكونك من عملائنا المميزين في متجر {{2}}.\n\n"
                            "يسعدنا أن نقدم لك عرضاً حصرياً:\n\n"
                            "{{3}}\n\n"
                            "نتمنى لك تجربة تسوق ممتعة دائماً 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                ],
            },
            "en": {
                "template_name": "vip_reward_en",
                "slots":         ["customer_name", "store_name", "vip_coupon"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👑\n\n"
                            "Thank you for being one of our valued customers at {{2}}.\n\n"
                            "Here's an exclusive reward just for you:\n\n"
                            "{{3}}\n\n"
                            "Wishing you a delightful shopping experience 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                ],
            },
        },
    },
}


# ── Lookup helpers ────────────────────────────────────────────────────────

def iter_template_seeds(language: str = "ar") -> List[Dict[str, Any]]:
    """
    Return one seed dict per default template for the requested language.
    Shape matches the existing `SEED_TEMPLATES` list used by
    `_seed_templates_if_empty`, so the seeder can splice these in.
    """
    out: List[Dict[str, Any]] = []
    for feature_key, spec in DEFAULT_AUTOMATION_TEMPLATES.items():
        lang_spec = spec["languages"].get(language)
        if not lang_spec:
            continue
        out.append({
            "name":          lang_spec["template_name"],
            "language":      language,
            "category":      spec["category"],
            "status":        "APPROVED",       # seed-only; real templates get
                                                # APPROVED via Meta sync.
            "components":    lang_spec["components"],
            "feature_key":   feature_key,
            "slots":         lang_spec["slots"],
        })
    return out


def numeric_var_map_for(template_name: str) -> Dict[str, str]:
    """
    Return `{"{{1}}": "customer_name", "{{2}}": ...}` for the named template,
    or {} if it isn't part of the default library.

    Returns ONLY the BODY positional contract — never includes URL-button
    slots. Mixing the two was the historical root cause of Meta error
    132000 (number of body parameters did not match the BODY's actual
    placeholder count): if a template's BODY has 1 placeholder and the
    button has 1 placeholder, we used to flatten both into a single
    list of length 2 and shove all of them into the body component.

    Order contract:
      * Prefer `body_slots` when defined — slot[0] → {{1}}, slot[1] → {{2}}, …
      * Fall back to legacy `slots` for older specs that haven't been
        migrated. This keeps templates without explicit `body_slots`
        working unchanged.

    Button URL parameters are resolved separately by the engine via
    `_URL_SLOT_PRECEDENCE` in `automation_engine.py`.
    """
    for spec in DEFAULT_AUTOMATION_TEMPLATES.values():
        for lang_spec in spec["languages"].values():
            if lang_spec["template_name"] == template_name:
                body_slots = lang_spec.get("body_slots")
                if body_slots is None:
                    # Legacy spec — assume all `slots` are body slots, the
                    # historical (buggy) behaviour. New specs override this
                    # by declaring `body_slots` explicitly.
                    body_slots = lang_spec.get("slots", [])
                return {
                    f"{{{{{i + 1}}}}}": slot
                    for i, slot in enumerate(body_slots)
                }
    return {}


def feature_for_template(template_name: str) -> Dict[str, Any] | None:
    """Return the parent feature spec for a default template, or None."""
    for feature_key, spec in DEFAULT_AUTOMATION_TEMPLATES.items():
        for lang_spec in spec["languages"].values():
            if lang_spec["template_name"] == template_name:
                return {"feature_key": feature_key, **spec}
    return None


def required_slots_for(template_name: str) -> List[str]:
    """
    Return the ordered list of named slots a default template requires.
    Empty if the template is not in the default library — merchant-authored
    templates can use any variables they want, subject to placeholder
    integrity (which is enforced separately).
    """
    for spec in DEFAULT_AUTOMATION_TEMPLATES.values():
        for lang_spec in spec["languages"].values():
            if lang_spec["template_name"] == template_name:
                return list(lang_spec["slots"])
    return []
