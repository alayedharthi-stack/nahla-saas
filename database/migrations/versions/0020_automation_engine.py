"""Add trigger_event to smart_automations and create automation_executions table.

Revision ID: 0020
Revises: 0019
Create Date: 2026-04-15

Idempotency (F16)
─────────────────
Some databases were bootstrapped via ``Base.metadata.create_all()`` while
``alembic_version`` still lags behind 0020. ``trigger_event`` and
``automation_executions`` may already exist without the migration-owned
indexes. Inspector-guarded DDL skips present objects and adds any missing
pieces; clean upgrades are unchanged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

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


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    # ── smart_automations: add trigger_event column ───────────────────────────
    if not _has_column(bind, "smart_automations", "trigger_event"):
        op.add_column(
            "smart_automations",
            sa.Column("trigger_event", sa.String(), nullable=True),
        )
    if not _has_index(bind, "smart_automations", "ix_smart_automations_trigger_event"):
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
    if not _has_table(bind, "automation_executions"):
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
    if not _has_index(bind, "automation_executions", "ix_automation_executions_event_automation"):
        op.create_index(
            "ix_automation_executions_event_automation",
            "automation_executions",
            ["event_id", "automation_id"],
            unique=True,  # idempotency: one execution record per (event, automation) pair
        )
    if not _has_index(bind, "automation_executions", "ix_automation_executions_tenant_id"):
        op.create_index(
            "ix_automation_executions_tenant_id",
            "automation_executions",
            ["tenant_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "automation_executions", "ix_automation_executions_tenant_id"):
        op.drop_index("ix_automation_executions_tenant_id", table_name="automation_executions")
    if _has_index(bind, "automation_executions", "ix_automation_executions_event_automation"):
        op.drop_index(
            "ix_automation_executions_event_automation",
            table_name="automation_executions",
        )
    if _has_table(bind, "automation_executions"):
        op.drop_table("automation_executions")
    if _has_index(bind, "smart_automations", "ix_smart_automations_trigger_event"):
        op.drop_index(
            "ix_smart_automations_trigger_event",
            table_name="smart_automations",
        )
    if _has_column(bind, "smart_automations", "trigger_event"):
        op.drop_column("smart_automations", "trigger_event")
