"""
core/automations_seed.py
────────────────────────
Canonical automation seeder — single source of truth.

Two routers used to each seed SmartAutomation rows independently:
  • routers/automations.py  → 6 "marketing" automations (abandoned_cart, ...)
  • routers/intelligence.py → 3 "zombie" automations (cart_recovery, ...)

The zombie rows were never wired to any trigger (trigger_event=NULL) and were
invisible in the UI. They are removed in migration 0024; this module now
owns the one legitimate seed list and sets `trigger_event` at creation time
so new tenants don't inherit the old NULL bug.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from sqlalchemy.orm.attributes import flag_modified

from core.automation_triggers import (
    AUTOMATION_TYPE_TO_TRIGGER,
    AutomationTrigger,
)
from models import Promotion, SmartAutomation


# Canonical engine assignment per automation_type. Mirrors the backfill table
# in `database/migrations/versions/0027_automation_engine_column.py` and is
# used both by `seed_automations_if_empty` (for new tenants) and by
# `ensure_engine_for_tenant` (defensive runtime repair after the migration).
ENGINE_BY_TYPE: Dict[str, str] = {
    "abandoned_cart":        "recovery",
    "customer_winback":      "recovery",
    "unpaid_order_reminder": "recovery",
    "cod_confirmation":      "recovery",
    "vip_upgrade":           "growth",
    "predictive_reorder":    "growth",
    "new_product_alert":     "growth",
    "back_in_stock":         "growth",
    "seasonal_offer":        "growth",
    "salary_payday_offer":   "growth",
}


# Canonical seed list. Each row carries its trigger_event AND its engine so
# the automation engine can match it immediately on creation and the merchant
# dashboard can render it inside the right operational bucket — no backfill
# migration needed for fresh tenants.
SEED_AUTOMATIONS: List[Dict[str, Any]] = [
    {
        "automation_type": "abandoned_cart",
        "engine":          "recovery",
        "trigger_event":   AutomationTrigger.CART_ABANDONED.value,
        "name":            "استرداد العربة المتروكة",
        "enabled":         False,
        "config": {
            # ── Three-stage recovery workflow ───────────────────────────────
            # Stage 1 (30 min)   — friendly reminder, NO discount.
            # Stage 2 (6 h)      — empathetic urgency, "need help?", NO discount.
            # Stage 3 (24 h)     — last chance with a real coupon.
            #
            # Stages 2 and 3 are NOT triggered by the storefront snippet —
            # only stage 1 is. The follow-ups are emitted by
            # `automation_emitters.scan_abandoned_cart_followups`, which
            # writes a NEW `cart_abandoned` AutomationEvent carrying
            # `payload.step_idx = 1` (stage 2) or `step_idx = 2` (stage 3).
            # The engine honours that explicit index instead of recomputing
            # from event age (see `_active_step_for_event`), so each stage
            # gets its own AutomationExecution row, its own template, and
            # its own coupon decision.
            #
            # AI gating: stage 3 sets `auto_coupon=true`. For tenants on
            # OFF, that always issues a coupon from the pool. For tenants
            # on ADVISORY/ENFORCE the OfferDecisionService takes over and
            # may return SOURCE_NONE (no coupon) for low-value carts or
            # customers who already received one this week — that is the
            # "AI may decide depending on customer value or cart size"
            # contract from the product spec.
            "steps": [
                {
                    "delay_minutes": 30,
                    "message_type":  "reminder",
                    "template_name":    "abandoned_cart_recovery_ar",
                    "template_name_en": "abandoned_cart_recovery_en",
                },
                {
                    "delay_minutes": 360,
                    "message_type":  "reminder",
                    "template_name":    "abandoned_cart_followup_ar",
                    "template_name_en": "abandoned_cart_followup_en",
                },
                {
                    "delay_minutes": 1440,
                    "message_type":  "coupon",
                    "auto_coupon":   True,
                    "template_name":    "abandoned_cart_final_offer_ar",
                    "template_name_en": "abandoned_cart_final_offer_en",
                },
            ],
            # `template_name` is kept as the default for the legacy
            # single-template execution path, used as fallback when a
            # step does not declare its own template_name.
            "template_name":    "abandoned_cart_recovery_ar",
            "template_name_en": "abandoned_cart_recovery_en",
            "language":         "ar",
        },
    },
    {
        "automation_type": "predictive_reorder",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.PREDICTIVE_REORDER_DUE.value,
        "name":            "تذكير إعادة الطلب التنبؤي",
        "enabled":         False,
        "config": {
            "template_name": "predictive_reorder_reminder_ar",
            "var_map": {"{{1}}": "customer_name", "{{2}}": "product_name", "{{3}}": "reorder_url"},
            "days_before": 3,
        },
    },
    {
        "automation_type": "customer_winback",
        "engine":          "recovery",
        "trigger_event":   AutomationTrigger.CUSTOMER_INACTIVE.value,
        "name":            "استرجاع العملاء غير النشطين",
        "enabled":         False,
        "config": {
            "inactive_days_first":  60,
            "inactive_days_second": 90,
            "discount_pct":         15,
            "auto_coupon":          True,   # pull a real code from the pool
            "template_name":        "win_back_ar",
            "template_name_en":     "win_back_en",
            "language":             "ar",
            # Payload-condition guard: only run when the status transition
            # actually landed the customer in an inactive/at_risk bucket.
            "conditions": {
                "payload": {"to": ["inactive", "at_risk"]},
            },
        },
    },
    {
        "automation_type": "vip_upgrade",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.VIP_CUSTOMER_UPGRADE.value,
        "name":            "مكافأة عملاء VIP",
        "enabled":         False,
        "config": {
            "min_spent_sar":    2000,
            "discount_pct":     20,
            "auto_coupon":      True,        # pull a VIP-tier code from the pool
            "template_name":    "vip_reward_ar",
            "template_name_en": "vip_reward_en",
            "language":         "ar",
            "conditions": {
                "payload": {"to": "vip"},
            },
        },
    },
    {
        "automation_type": "new_product_alert",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.PRODUCT_CREATED.value,
        "name":            "تنبيه المنتجات الجديدة",
        "enabled":         False,
        "config": {
            "target_interested_only": True,
            "template_name":          "new_arrivals",
        },
    },
    {
        "automation_type": "back_in_stock",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.PRODUCT_BACK_IN_STOCK.value,
        "name":            "تنبيه عودة المنتج للمخزون",
        "enabled":         False,
        "config": {
            # Fan-out is handled upstream by store_sync, which emits one
            # AutomationEvent per pending ProductInterest row. The engine
            # then renders the back_in_stock_{ar,en} template (named slots:
            # customer_name, store_name, product_url) for each of those
            # events. There are no delay steps and no condition payload.
            "template_name":    "back_in_stock_ar",
            "template_name_en": "back_in_stock_en",
            "language":         "ar",
        },
    },
    {
        "automation_type": "unpaid_order_reminder",
        "engine":          "recovery",
        "trigger_event":   AutomationTrigger.ORDER_PAYMENT_PENDING.value,
        "name":            "تذكير الطلبات غير المدفوعة",
        "enabled":         False,
        "config": {
            # Three escalating reminders for orders left in pending /
            # awaiting_payment after their grace period. The emitter
            # (`automation_emitters.scan_unpaid_orders`) re-emits an event
            # every step interval; the engine deduplicates per (order, step)
            # via AutomationExecution.event_id idempotency.
            "language":          "ar",
            "template_name":     "unpaid_order_reminder_ar",
            "template_name_en":  "unpaid_order_reminder_en",
            "grace_minutes":     60,
            "steps": [
                {"delay_minutes": 60,   "message_type": "reminder"},
                {"delay_minutes": 360,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "final"},
            ],
            "stop_on_payment": True,
            "stop_on_reply":   True,
        },
    },
    {
        "automation_type": "cod_confirmation",
        "engine":          "recovery",
        "trigger_event":   AutomationTrigger.ORDER_COD_PENDING.value,
        "name":            "تأكيد الطلبات (الدفع عند الاستلام)",
        # Disabled by default like every other recovery automation.
        # Toggling it ON does NOT push existing orders into the flow;
        # the sweeper only acts on orders that landed in
        # `pending_confirmation` after the toggle.
        "enabled":         False,
        "config": {
            # ── COD confirmation workflow ──────────────────────────────
            # Initial confirmation template is sent SYNCHRONOUSLY by
            # `routers/ai_sales.py::ai_sales_create_order` on order
            # creation (cod_order_confirmation_ar with two QUICK_REPLY
            # buttons). This automation owns the *follow-up* half:
            #
            #   T+0      synchronous template (handled outside this
            #            automation, by ai_sales_create_order)
            #   T+6 h    reminder if order still in `pending_confirmation`
            #   T+24 h   auto-cancel the order, fire a final notice
            #
            # The sweeper (`automation_emitters.scan_cod_confirmations`)
            # walks `Order.status == pending_confirmation` for tenants
            # with this automation enabled, emits ORDER_COD_PENDING with
            # `step_idx` for the reminder, and performs the cancel
            # transition itself (cancel is a state mutation, not a
            # WhatsApp send, so it doesn't go through the engine).
            #
            # Conflict-free with `unpaid_order_reminder`: that sweeper
            # operates on `_PENDING_PAYMENT_STATUSES` (pending /
            # awaiting_payment / draft / new), which deliberately does
            # NOT include `pending_confirmation`. A COD order never
            # appears in both queues at once.
            "language":            "ar",
            "template_name":       "cod_confirmation_reminder_ar",
            "template_name_en":    "cod_confirmation_reminder_en",
            "reminder_after_minutes": 360,    # 6 hours
            "cancel_after_minutes":   1440,   # 24 hours total
            "steps": [
                {
                    "delay_minutes": 360,
                    "message_type":  "reminder",
                    "template_name":    "cod_confirmation_reminder_ar",
                    "template_name_en": "cod_confirmation_reminder_en",
                },
            ],
        },
    },
    {
        "automation_type": "seasonal_offer",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.SEASONAL_EVENT_DUE.value,
        "name":            "عروض المناسبات الذكية",
        "enabled":         False,
        "config": {
            # Calendar-driven. `automation_emitters.scan_calendar_events`
            # emits SEASONAL_EVENT_DUE one day before each entry in the
            # built-in Saudi calendar (national_day, founding_day, ramadan,
            # eid_fitr, eid_adha, white_friday). The merchant configures the
            # offer once and the engine fires it for every applicable event.
            "language":         "ar",
            "template_name":    "seasonal_offer_ar",
            "template_name_en": "seasonal_offer_en",
            "discount_pct":     15,
            # Promotion-backed: the engine reads `promotion_id` from the
            # tenant's auto-seeded "Seasonal — 15% off" promotion (created
            # on first toggle by `ensure_default_promotions_for_tenant`)
            # and materialises a personal NHxxx code per recipient. Falls
            # back gracefully to no discount if no promotion is configured.
            "discount_source":  "promotion",
            "default_promotion_slug": "seasonal_default_15",
            "audience": {
                # Optional segment filter; default broadcasts to everyone
                # who has bought at least once.
                "min_orders": 1,
            },
        },
    },
    {
        "automation_type": "salary_payday_offer",
        "engine":          "growth",
        "trigger_event":   AutomationTrigger.SALARY_PAYDAY_DUE.value,
        "name":            "عروض الرواتب",
        "enabled":         False,
        "config": {
            # Default payday in Saudi private sector is the 27th of the
            # Gregorian month. Emitted one day before so customers see the
            # offer when their salary lands. Configurable per tenant via
            # `payday_day` (1-31).
            "language":         "ar",
            "template_name":    "salary_payday_offer_ar",
            "template_name_en": "salary_payday_offer_en",
            "payday_day":       27,
            "discount_pct":     10,
            # Promotion-backed (see seasonal_offer above for the rationale).
            "discount_source":  "promotion",
            "default_promotion_slug": "salary_payday_default_10",
            "audience": {
                # Default targeting: customers who have bought at least
                # once and are not currently inactive (>120 days silent).
                "min_orders":          1,
                "max_inactive_days":   120,
            },
        },
    },
]


def seed_automations_if_empty(db: Session, tenant_id: int) -> None:
    """
    Idempotent seed for one tenant.

    On first call for a tenant this inserts the 6 canonical automations with
    `trigger_event` pre-populated. On subsequent calls it inserts anything
    missing (e.g. after we add a new trigger to the enum) without touching
    existing rows the merchant may have customised.
    """
    existing_types = {
        t for (t,) in db.query(SmartAutomation.automation_type)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .all()
    }
    now = datetime.now(timezone.utc)
    for seed in SEED_AUTOMATIONS:
        if seed["automation_type"] in existing_types:
            continue
        auto = SmartAutomation(
            tenant_id=tenant_id,
            automation_type=seed["automation_type"],
            engine=seed.get("engine") or ENGINE_BY_TYPE.get(seed["automation_type"], "recovery"),
            trigger_event=seed["trigger_event"],
            name=seed["name"],
            enabled=seed["enabled"],
            config=seed["config"],
            created_at=now,
            updated_at=now,
        )
        db.add(auto)
    db.flush()


def ensure_engine_for_tenant(db: Session, tenant_id: int) -> int:
    """
    Defensive runtime repair: if any SmartAutomation row for this tenant has
    an unknown / NULL `engine`, set it from the canonical ENGINE_BY_TYPE map.

    The 0027 migration already backfills production rows once, but in tests
    that build the schema with `Base.metadata.create_all` (no Alembic) the
    `engine` column is added with its model default of "recovery", which is
    wrong for growth-engine rows like vip_upgrade. Calling this from the
    `/automations/engines/summary` and `/automations` handlers keeps both
    paths consistent without forcing every test to run the migration.

    Returns the number of rows repaired.
    """
    rows = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .all()
    )
    repaired = 0
    for r in rows:
        canonical = ENGINE_BY_TYPE.get(r.automation_type)
        if canonical is None:
            continue
        if (r.engine or "").strip() == canonical:
            continue
        r.engine = canonical
        repaired += 1
    if repaired:
        db.flush()
    return repaired


# ─────────────────────────────────────────────────────────────────────────────
# Default Promotions
# ─────────────────────────────────────────────────────────────────────────────
# Each `default_promotion_slug` referenced in SEED_AUTOMATIONS above maps to
# one entry here. We seed the row in `draft` status so the merchant has to
# review and activate it explicitly — the automation may be enabled while the
# promotion is still draft, in which case `materialise_for_customer` returns
# None and the WhatsApp send falls back to a discount-less template.
#
# Slug → terms. The slug is stored in `Promotion.extra_metadata.slug` and
# also in the automation's `config.default_promotion_slug` so the linker
# below can rebind the IDs after re-seeding.
#
# ── Seasonal split ──────────────────────────────────────────────────────────
# The `seasonal_offer` automation is calendar-driven and fans out across
# every entry in `core/calendar_events.py` (founding_day, national_day,
# white_friday, ramadan_start, eid_al_fitr, eid_al_adha). The merchant
# expects to set the discount *per occasion* (Founding Day at 15% feels
# different from White Friday at 25%), so we seed a dedicated Promotion
# row per occasion tagged with `extra_metadata.occasion_slug` matching
# the calendar event slug. The engine prefers the occasion-specific
# Promotion at materialisation time and falls back to the generic
# `seasonal_default_15` row if a tenant deletes / pauses the per-occasion
# entry — that fallback is what `seasonal_offer.config.promotion_id`
# points at, so the legacy single-promotion link keeps working untouched.
DEFAULT_PROMOTIONS: Dict[str, Dict[str, Any]] = {
    # Generic fallback. Linked to `seasonal_offer.config.promotion_id`.
    # Kept so a tenant who deletes every per-occasion row still has a
    # working seasonal automation — never auto-deleted.
    "seasonal_default_15": {
        "name":           "خصم المناسبات — 15% (افتراضي)",
        "description":    "احتياطي تلقائي 15% يستخدمه أتمتة المناسبات حين لا يجد عرضاً مخصّصاً للمناسبة الحالية.",
        "promotion_type": "percentage",
        "discount_value": 15.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"role": "seasonal_fallback"},
    },
    # Per-occasion seasonal Promotions. Each carries `occasion_slug` which
    # the engine matches against the SEASONAL_EVENT_DUE event payload's
    # `event_slug`. These are first-class merchant-editable rows that the
    # Promotions page surfaces inside the Seasonal Calendar panel.
    "seasonal_founding_day_15": {
        "name":           "عرض يوم التأسيس — 15%",
        "description":    "خصم 15% للاحتفاء بيوم التأسيس السعودي (22 فبراير). يُفعِّله الطيار الآلي تلقائياً قبل اليوم بيوم.",
        "promotion_type": "percentage",
        "discount_value": 15.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"occasion_slug": "founding_day", "kind": "seasonal"},
    },
    "seasonal_national_day_15": {
        "name":           "عرض اليوم الوطني — 15%",
        "description":    "خصم 15% لليوم الوطني السعودي (23 سبتمبر). الطيار الآلي يُرسل كوداً شخصياً لكل عميل مؤهل.",
        "promotion_type": "percentage",
        "discount_value": 15.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"occasion_slug": "national_day", "kind": "seasonal"},
    },
    "seasonal_white_friday_25": {
        "name":           "عرض الجمعة البيضاء — 25%",
        "description":    "خصم 25% في موسم الجمعة البيضاء (آخر جمعة من نوفمبر). أعلى نسبة موصى بها للموسم التجاري الأكبر.",
        "promotion_type": "percentage",
        "discount_value": 25.0,
        "conditions":     {},
        "extra_metadata": {"occasion_slug": "white_friday", "kind": "seasonal"},
    },
    "seasonal_ramadan_20": {
        "name":           "عرض رمضان — 20%",
        "description":    "خصم 20% لبداية شهر رمضان. الكود يتم تجهيزه قبل ليلة رمضان ويُرسل تلقائياً عند بدء الشهر.",
        "promotion_type": "percentage",
        "discount_value": 20.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"occasion_slug": "ramadan_start", "kind": "seasonal"},
    },
    "seasonal_eid_fitr_15": {
        "name":           "عرض عيد الفطر — 15%",
        "description":    "خصم 15% بمناسبة عيد الفطر. مناسب للهدايا والملابس والاحتياجات الاستهلاكية.",
        "promotion_type": "percentage",
        "discount_value": 15.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"occasion_slug": "eid_al_fitr", "kind": "seasonal"},
    },
    "seasonal_eid_adha_15": {
        "name":           "عرض عيد الأضحى — 15%",
        "description":    "خصم 15% بمناسبة عيد الأضحى.",
        "promotion_type": "percentage",
        "discount_value": 15.0,
        "conditions":     {"min_orders_for_eligibility": 1},
        "extra_metadata": {"occasion_slug": "eid_al_adha", "kind": "seasonal"},
    },
    # Salary payday is wired through its own `salary_payday_offer`
    # automation (not the seasonal one), so it stays as a single row
    # without an `occasion_slug` tag — the link is via the automation's
    # `config.promotion_id` only.
    "salary_payday_default_10": {
        "name":           "عرض يوم الراتب — 10%",
        "description":    "خصم تلقائي 10% يُولِّد كوبوناً شخصياً يوم الراتب (يوم 27 من كل شهر افتراضياً).",
        "promotion_type": "percentage",
        "discount_value": 10.0,
        "conditions":     {},
        "extra_metadata": {"role": "salary_payday"},
    },
}


# Public catalogue of seasonal occasions exposed by the Seasonal Calendar
# panel on the Promotions page. Each tuple binds:
#   • occasion_slug      — matches `core/calendar_events.CalendarEvent.slug`
#                          AND `Promotion.extra_metadata.occasion_slug`
#   • promotion_slug     — `Promotion.extra_metadata.slug` of the seeded row
#   • automation_type    — which SmartAutomation surface fans this out
#   • ai_summary         — one-line rendered next to the card, explains
#                          what the AI does once the merchant turns it on
#
# Order is the order the cards appear in the dashboard (calendar order).
SEASONAL_OCCASIONS: List[Dict[str, str]] = [
    {
        "occasion_slug":   "founding_day",
        "promotion_slug":  "seasonal_founding_day_15",
        "automation_type": "seasonal_offer",
        "ai_summary":      "يُجهِّز الطيار الآلي الكود قبل 22 فبراير بيوم ويُرسله تلقائياً للعملاء الذين أكملوا طلباً واحداً على الأقل.",
    },
    {
        "occasion_slug":   "ramadan_start",
        "promotion_slug":  "seasonal_ramadan_20",
        "automation_type": "seasonal_offer",
        "ai_summary":      "ينطلق قبل ليلة رمضان مباشرة. يختار الذكاء الاصطناعي العملاء الأكثر تفاعلاً ويُولّد كوداً شخصياً لكل واحد.",
    },
    {
        "occasion_slug":   "eid_al_fitr",
        "promotion_slug":  "seasonal_eid_fitr_15",
        "automation_type": "seasonal_offer",
        "ai_summary":      "يُرسل قبل عيد الفطر بيوم. يستثني العملاء الذين تلقّوا كوبوناً نشطاً خلال الأسبوع الماضي تلقائياً.",
    },
    {
        "occasion_slug":   "eid_al_adha",
        "promotion_slug":  "seasonal_eid_adha_15",
        "automation_type": "seasonal_offer",
        "ai_summary":      "يُرسل قبل عيد الأضحى بيوم. الذكاء الاصطناعي يضبط ساعة الإرسال حسب أوقات تفاعل المتجر.",
    },
    {
        "occasion_slug":   "national_day",
        "promotion_slug":  "seasonal_national_day_15",
        "automation_type": "seasonal_offer",
        "ai_summary":      "يُرسل قبل 23 سبتمبر بيوم. يُولِّد كوداً شخصياً لكل عميل ويتبع نتائج الاسترداد ضمن لوحة الأداء.",
    },
    {
        "occasion_slug":   "white_friday",
        "promotion_slug":  "seasonal_white_friday_25",
        "automation_type": "seasonal_offer",
        "ai_summary":      "ينطلق قبل آخر جمعة من نوفمبر بيوم. أكبر موسم تجاري في السنة — الذكاء الاصطناعي يُرتب الإرسال حسب احتمال الشراء.",
    },
    # Salary payday has its own automation surface but lives in the same
    # calendar panel for merchant clarity. `occasion_slug` here is a
    # synthetic id (no matching CalendarEvent) — the API layer recognises
    # it and routes the linkage through `salary_payday_offer` instead.
    {
        "occasion_slug":   "salary_payday",
        "promotion_slug":  "salary_payday_default_10",
        "automation_type": "salary_payday_offer",
        "ai_summary":      "ينطلق يوم الراتب من كل شهر (افتراضياً 27). الذكاء الاصطناعي يستهدف العملاء غير الخاملين تلقائياً.",
    },
]


def ensure_default_promotions_for_tenant(db: Session, tenant_id: int) -> int:
    """
    Seed the catalog of default promotions referenced by SEED_AUTOMATIONS,
    then link each promotion-backed automation to its default promotion by
    writing `config.promotion_id` if it is missing.

    Idempotent on both sides:
      • Promotions are matched by `extra_metadata.slug` → never duplicated.
      • Automations are only updated when `config.promotion_id` is unset.

    Called from the engine before processing events so a freshly seeded
    tenant gets a working promotion-backed automation on first activation
    without any merchant action. Returns the number of mutations applied
    (rows created + automations re-linked).
    """
    mutations = 0

    existing_by_slug: Dict[str, Promotion] = {}
    for promo in db.query(Promotion).filter(Promotion.tenant_id == tenant_id).all():
        slug = (promo.extra_metadata or {}).get("slug")
        if slug:
            existing_by_slug[slug] = promo

    now = datetime.now(timezone.utc)
    for slug, spec in DEFAULT_PROMOTIONS.items():
        if slug in existing_by_slug:
            continue
        # Spec-level extras (e.g. `occasion_slug`, `kind`, `role`) ride
        # alongside the seeder bookkeeping so the engine and the dashboard
        # can match per-occasion rows without a follow-up read.
        extra_metadata: Dict[str, Any] = dict(spec.get("extra_metadata") or {})
        extra_metadata.update({
            "slug":           slug,
            "managed_by":     "automations_seed",
            "auto_seeded_at": now.isoformat(),
        })
        promo = Promotion(
            tenant_id=tenant_id,
            name=spec["name"],
            description=spec.get("description"),
            promotion_type=spec["promotion_type"],
            discount_value=spec.get("discount_value"),
            conditions=spec.get("conditions") or {},
            status="draft",
            usage_count=0,
            extra_metadata=extra_metadata,
            created_at=now.replace(tzinfo=None),
            updated_at=now.replace(tzinfo=None),
        )
        db.add(promo)
        db.flush()
        existing_by_slug[slug] = promo
        mutations += 1

    automations = (
        db.query(SmartAutomation)
        .filter(SmartAutomation.tenant_id == tenant_id)
        .all()
    )
    for auto in automations:
        cfg = dict(auto.config or {})
        if cfg.get("discount_source") != "promotion":
            continue
        if cfg.get("promotion_id"):
            continue
        slug = cfg.get("default_promotion_slug")
        if not slug:
            continue
        promo = existing_by_slug.get(slug)
        if promo is None:
            continue
        cfg["promotion_id"] = promo.id
        auto.config = cfg
        flag_modified(auto, "config")
        mutations += 1

    if mutations:
        db.flush()
    return mutations


def ensure_trigger_event_for_tenant(db: Session, tenant_id: int) -> int:
    """
    Defensive runtime repair: if any SmartAutomation row for this tenant has
    a NULL or empty `trigger_event`, fill it in using the canonical mapping
    of `automation_type` → `AutomationTrigger`.

    Called at the start of each tenant's engine cycle so a missed migration
    can't disable an entire tenant silently. Returns the number of rows
    repaired.

    IMPORTANT: This function only touches rows whose `trigger_event` is NULL
    or empty. Rows with an explicit `trigger_event` — even one that doesn't
    match our canonical enum — are left alone, because a merchant or
    integration test may legitimately wire an automation to a custom event
    name (e.g. `order_created`, `order_paid`) that we don't manage centrally.
    Overwriting those would silently break their automations.
    """
    rows = (
        db.query(SmartAutomation)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.automation_type.in_(list(AUTOMATION_TYPE_TO_TRIGGER.keys())),
        )
        .all()
    )
    repaired = 0
    for r in rows:
        current = (r.trigger_event or "").strip()
        if current:
            continue
        r.trigger_event = AUTOMATION_TYPE_TO_TRIGGER[r.automation_type].value
        repaired += 1
    if repaired:
        db.flush()
    return repaired
