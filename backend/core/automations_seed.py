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

from core.automation_triggers import (
    AUTOMATION_TYPE_TO_TRIGGER,
    AutomationTrigger,
)
from models import SmartAutomation


# Canonical seed list. Each row carries its trigger_event so the automation
# engine can match it immediately on creation — no backfill migration needed
# for fresh tenants.
SEED_AUTOMATIONS: List[Dict[str, Any]] = [
    {
        "automation_type": "abandoned_cart",
        "trigger_event":   AutomationTrigger.CART_ABANDONED.value,
        "name":            "استرداد العربة المتروكة",
        "enabled":         False,
        "config": {
            # Three-stage recovery flow: 30 min nudge, 6 h nudge, 24 h coupon.
            # Stage 3 sets `auto_coupon=true` so the engine pulls a real
            # discount code from the merchant's coupon pool at send time
            # (NHxxx format, Salla-synced, percentage discount, 48h expiry).
            "steps": [
                {"delay_minutes": 30,   "message_type": "reminder"},
                {"delay_minutes": 360,  "message_type": "reminder"},
                {"delay_minutes": 1440, "message_type": "coupon", "auto_coupon": True},
            ],
            "template_name":    "abandoned_cart_recovery_ar",
            "template_name_en": "abandoned_cart_recovery_en",
            "language":         "ar",
        },
    },
    {
        "automation_type": "predictive_reorder",
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
            trigger_event=seed["trigger_event"],
            name=seed["name"],
            enabled=seed["enabled"],
            config=seed["config"],
            created_at=now,
            updated_at=now,
        )
        db.add(auto)
    db.flush()


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
