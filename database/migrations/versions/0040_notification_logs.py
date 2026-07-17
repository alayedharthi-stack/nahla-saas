"""Create notification_logs table for smart email throttling.

Revision ID: 0040
Revises: 0039

Goal:
  - Track every email notification attempt (sent / skipped) per customer per tenant.
  - Used by _should_notify_merchant_email() to prevent email spam.
  - Exposed via GET /merchant/notification-logs for merchant visibility.

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
the table or indexes while ``alembic_version`` is still at 0039.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_index, has_table

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if not has_table(bind, "notification_logs"):
        op.create_table(
            "notification_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("event", sa.String(60), nullable=False),
            sa.Column("status", sa.String(10), nullable=False),
            sa.Column("reason", sa.String(255), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    for index_name, columns in (
        ("ix_notif_log_tenant_created", ("tenant_id", "created_at")),
        ("ix_notif_log_tenant_cust_event", ("tenant_id", "customer_id", "event")),
    ):
        if has_table(bind, "notification_logs") and not has_index(
            bind, "notification_logs", index_name,
        ):
            op.create_index(index_name, "notification_logs", list(columns))


def downgrade() -> None:
    op.drop_index("ix_notif_log_tenant_cust_event", table_name="notification_logs")
    op.drop_index("ix_notif_log_tenant_created", table_name="notification_logs")
    op.drop_table("notification_logs")
