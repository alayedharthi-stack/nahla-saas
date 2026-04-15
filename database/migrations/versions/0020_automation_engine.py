"""Add trigger_event to smart_automations and create automation_executions table.

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Maps existing automation_type values to the AutomationEvent.event_type that triggers them.
_TYPE_TO_EVENT = {
    "abandoned_cart":    "cart_abandoned",
    "customer_winback":  "customer_status_changed",
    "vip_upgrade":       "customer_status_changed",
    "new_product_alert": "order_created",
    "back_in_stock":     "product_back_in_stock",
    # predictive_reorder keeps its existing job-based trigger — no event mapping
}


def upgrade() -> None:
    # ── smart_automations: add trigger_event column ───────────────────────────
    op.add_column(
        "smart_automations",
        sa.Column("trigger_event", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_smart_automations_trigger_event",
        "smart_automations",
        ["trigger_event"],
    )

    # Backfill trigger_event from automation_type
    conn = op.get_bind()
    for atype, event_type in _TYPE_TO_EVENT.items():
        conn.execute(
            sa.text(
                "UPDATE smart_automations "
                "SET trigger_event = :event_type "
                "WHERE automation_type = :atype AND trigger_event IS NULL"
            ),
            {"event_type": event_type, "atype": atype},
        )

    # ── automation_executions table ───────────────────────────────────────────
    op.create_table(
        "automation_executions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("automation_id", sa.Integer(), sa.ForeignKey("smart_automations.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("automation_events.id"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False),        # sent | skipped | failed
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("action_taken", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_automation_executions_event_automation",
        "automation_executions",
        ["event_id", "automation_id"],
        unique=True,  # idempotency: one execution record per (event, automation) pair
    )
    op.create_index(
        "ix_automation_executions_tenant_id",
        "automation_executions",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_automation_executions_tenant_id")
    op.drop_index("ix_automation_executions_event_automation")
    op.drop_table("automation_executions")
    op.drop_index("ix_smart_automations_trigger_event")
    op.drop_column("smart_automations", "trigger_event")
