"""
core/automation_triggers.py
───────────────────────────
Single source of truth for automation trigger names used across Nahla.

Historical background (why this file exists)
────────────────────────────────────────────
Until April 2026 the UI, backend emitters and DB-stored SmartAutomation rows
used three different naming conventions for the same 6 logical triggers:

  UI label            backend emitted        SmartAutomation.trigger_event
  ─────────────────   ────────────────────   ──────────────────────────────
  cart_abandoned      abandoned_cart         cart_abandoned (sometimes)
  predictive_reorder  (none — no emitter)    NULL
  customer_inactive   customer_status_changed  customer_status_changed
  vip_customer_upgrade customer_status_changed customer_status_changed
  product_created     order_created          order_created (wrong mapping)
  product_back_in_stock (none)               product_back_in_stock

The engine matches events by exact string equality on `trigger_event`, so
this drift silently dropped almost every event.

Rule going forward:
  • Every `emit_automation_event(..., event_type=...)` call MUST pass a value
    from this Enum.
  • Every SmartAutomation row seeded by Nahla MUST have its `trigger_event`
    set to one of these values.
  • The UI label trigger constants in
    `dashboard/src/api/automations.ts::AUTOMATION_META` MUST equal these
    values exactly.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict


class AutomationTrigger(str, Enum):
    CART_ABANDONED         = "cart_abandoned"
    PREDICTIVE_REORDER_DUE = "predictive_reorder_due"
    CUSTOMER_INACTIVE      = "customer_inactive"
    VIP_CUSTOMER_UPGRADE   = "vip_customer_upgrade"
    PRODUCT_CREATED        = "product_created"
    PRODUCT_BACK_IN_STOCK  = "product_back_in_stock"
    # Recovery engine: pending order has not been paid after a delay.
    ORDER_PAYMENT_PENDING  = "order_payment_pending"
    # Growth engine: scheduled by automation_emitters.scan_calendar_events
    # one day before the configured holiday / promo date.
    SEASONAL_EVENT_DUE     = "seasonal_event_due"
    # Growth engine: scheduled by automation_emitters.scan_calendar_events
    # one day before each tenant's configured payday.
    SALARY_PAYDAY_DUE      = "salary_payday_due"


# Canonical mapping: SmartAutomation.automation_type → AutomationTrigger
# Used by the seeder and the backfill migration so every row gets the right
# trigger_event on creation.
AUTOMATION_TYPE_TO_TRIGGER: Dict[str, AutomationTrigger] = {
    "abandoned_cart":        AutomationTrigger.CART_ABANDONED,
    "predictive_reorder":    AutomationTrigger.PREDICTIVE_REORDER_DUE,
    "customer_winback":      AutomationTrigger.CUSTOMER_INACTIVE,
    "vip_upgrade":           AutomationTrigger.VIP_CUSTOMER_UPGRADE,
    "new_product_alert":     AutomationTrigger.PRODUCT_CREATED,
    "back_in_stock":         AutomationTrigger.PRODUCT_BACK_IN_STOCK,
    "unpaid_order_reminder": AutomationTrigger.ORDER_PAYMENT_PENDING,
    "seasonal_offer":        AutomationTrigger.SEASONAL_EVENT_DUE,
    "salary_payday_offer":   AutomationTrigger.SALARY_PAYDAY_DUE,
}


# Legacy automation_types created by the zombie seeder in
# `core/automations_seed.py` before unification — these must be removed.
LEGACY_ZOMBIE_AUTOMATION_TYPES = (
    "cart_recovery",
    "reorder_reminder",
    "welcome_message",
)


# Legacy event names previously emitted by other modules. The engine will
# silently alias them to the canonical name so in-flight events from older
# callers still resolve correctly during the rollout window.
LEGACY_EVENT_ALIASES: Dict[str, AutomationTrigger] = {
    "abandoned_cart":          AutomationTrigger.CART_ABANDONED,
    "storefront_cart_abandon": AutomationTrigger.CART_ABANDONED,
}
