"""Normalise SmartAutomation.trigger_event + purge zombie automations.

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-16

Why
───
Migration 0020 introduced `trigger_event` and backfilled it once, but:
  • `_seed_automations_if_empty` in routers/automations.py continued creating
    new rows without setting `trigger_event`, so every tenant onboarded after
    0020 got `trigger_event = NULL` and their engine could never match.
  • A parallel seeder in core/automations_seed.py created 3 additional rows
    per tenant with `automation_type IN (cart_recovery, reorder_reminder,
    welcome_message)`. None of these have a trigger_event and none are
    visible in the UI — pure dead rows, 12 in production today.
  • The migration mapped `new_product_alert → order_created` which fires a
    "new product" message on every new order — semantically wrong. The
    canonical mapping is `new_product_alert → product_created`.
  • The migration mapped `customer_winback → customer_status_changed` and
    `vip_upgrade → customer_status_changed`, but those automations now match
    on specific triggers (`customer_inactive`, `vip_customer_upgrade`) which
    the intelligence service emits alongside the generic one.
  • The migration left `predictive_reorder → NULL`. The canonical trigger is
    `predictive_reorder_due`.

What this migration does
────────────────────────
  1. Sets `trigger_event` to the canonical value for every existing row whose
     `automation_type` is one of the six canonical types, even if it was
     already set by 0020 (idempotent).
  2. Deletes the three zombie automation_types and any AutomationExecution
     rows that referenced them (cascade is not set in the ORM, so handle
     manually).
  3. Re-maps any unprocessed `abandoned_cart` AutomationEvent rows to
     `cart_abandoned` so nothing in flight gets dropped on deploy.

Rollback is a no-op beyond restoring the former trigger_event mapping for
existing rows — zombie rows are not recreated because they served no
purpose.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Canonical mapping — must stay in sync with
# backend/core/automation_triggers.py::AUTOMATION_TYPE_TO_TRIGGER
_CANONICAL_TRIGGERS = {
    "abandoned_cart":    "cart_abandoned",
    "predictive_reorder": "predictive_reorder_due",
    "customer_winback":  "customer_inactive",
    "vip_upgrade":       "vip_customer_upgrade",
    "new_product_alert": "product_created",
    "back_in_stock":     "product_back_in_stock",
}

_ZOMBIE_TYPES = ("cart_recovery", "reorder_reminder", "welcome_message")

# For rollback only: the previous mapping installed by 0020.
_PREVIOUS_TRIGGERS = {
    "abandoned_cart":    "cart_abandoned",
    "customer_winback":  "customer_status_changed",
    "vip_upgrade":       "customer_status_changed",
    "new_product_alert": "order_created",
    "back_in_stock":     "product_back_in_stock",
    "predictive_reorder": None,
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1) Normalise trigger_event on canonical rows.
    for automation_type, trigger in _CANONICAL_TRIGGERS.items():
        conn.exec_driver_sql(
            """
            UPDATE smart_automations
               SET trigger_event = %s,
                   updated_at    = NOW()
             WHERE automation_type = %s
               AND (trigger_event IS DISTINCT FROM %s)
            """,
            (trigger, automation_type, trigger),
        )

    # 2) Delete dependent execution/event rows for zombie automations, then
    #    the zombie automations themselves. Done in explicit order because
    #    there are no cascades declared in the ORM.
    zombie_ids = [
        row[0]
        for row in conn.exec_driver_sql(
            "SELECT id FROM smart_automations WHERE automation_type = ANY(%s)",
            (list(_ZOMBIE_TYPES),),
        ).fetchall()
    ]
    if zombie_ids:
        conn.exec_driver_sql(
            "DELETE FROM automation_executions WHERE automation_id = ANY(%s)",
            (zombie_ids,),
        )
        conn.exec_driver_sql(
            "DELETE FROM smart_automations WHERE id = ANY(%s)",
            (zombie_ids,),
        )

    # 3) Re-map any unprocessed in-flight events that were emitted under the
    #    legacy name `abandoned_cart`. Processed rows are left untouched so
    #    the audit trail stays intact.
    conn.exec_driver_sql(
        """
        UPDATE automation_events
           SET event_type = 'cart_abandoned'
         WHERE event_type = 'abandoned_cart'
           AND processed  = FALSE
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    # Restore the previous trigger_event mapping from 0020. We don't
    # resurrect the zombie rows because they served no purpose.
    for automation_type, trigger in _PREVIOUS_TRIGGERS.items():
        conn.exec_driver_sql(
            """
            UPDATE smart_automations
               SET trigger_event = %s,
                   updated_at    = NOW()
             WHERE automation_type = %s
            """,
            (trigger, automation_type),
        )
    # In-flight event re-mapping is not reversed: if the new name has
    # already been observed by a healthy engine, reverting would only
    # duplicate work.
