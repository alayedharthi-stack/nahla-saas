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
                "slots":         ["customer_name", "store_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "لاحظنا أنك أضفت بعض المنتجات إلى السلة في متجر {{2}} لكن لم تكتمل عملية الشراء.\n\n"
                            "يمكنك إكمال الطلب بسهولة من هنا:\n\n"
                            "{{3}}\n\n"
                            "إذا احتجت أي مساعدة يسعدنا خدمتك 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "إكمال الطلب", "url": "{{3}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "abandoned_cart_recovery_en",
                "slots":         ["customer_name", "store_name", "checkout_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "We noticed you added some items to your cart at {{2}} but didn't complete checkout.\n\n"
                            "You can finish your order here:\n\n"
                            "{{3}}\n\n"
                            "Reply to this message if you need any help 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Complete order", "url": "{{3}}"}],
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
                "slots":         ["customer_name", "order_id", "store_name", "payment_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "مرحباً {{1}} 👋\n\n"
                            "طلبك رقم #{{2}} في متجر {{3}} لا يزال بانتظار الدفع.\n\n"
                            "يمكنك إكمال الدفع الآن من هنا:\n\n"
                            "{{4}}\n\n"
                            "إذا واجهت أي مشكلة في الدفع نحن هنا لمساعدتك 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 نحلة — مساعد متجرك"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "إكمال الدفع", "url": "{{4}}"}],
                    },
                ],
            },
            "en": {
                "template_name": "unpaid_order_reminder_en",
                "slots":         ["customer_name", "order_id", "store_name", "payment_url"],
                "components": [
                    {
                        "type": "BODY",
                        "text": (
                            "Hi {{1}} 👋\n\n"
                            "Your order #{{2}} at {{3}} is still awaiting payment.\n\n"
                            "You can complete the payment here:\n\n"
                            "{{4}}\n\n"
                            "Reply if you ran into any trouble during checkout 🌟"
                        ),
                    },
                    {"type": "FOOTER", "text": "🐝 Nahla — your store assistant"},
                    {
                        "type": "BUTTONS",
                        "buttons": [{"type": "URL", "text": "Complete payment", "url": "{{4}}"}],
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

    The order of `slots` in `DEFAULT_AUTOMATION_TEMPLATES` is the contract:
    slot[0] → {{1}}, slot[1] → {{2}}, …
    """
    for spec in DEFAULT_AUTOMATION_TEMPLATES.values():
        for lang_spec in spec["languages"].values():
            if lang_spec["template_name"] == template_name:
                return {
                    f"{{{{{i + 1}}}}}": slot
                    for i, slot in enumerate(lang_spec["slots"])
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
