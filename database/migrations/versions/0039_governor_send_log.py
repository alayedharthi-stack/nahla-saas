"""Create governor_send_logs table for Global Send Governor.

Revision ID: 0039
Revises: 0038
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "governor_send_logs",
        sa.Column("id",             sa.Integer(),  primary_key=True),
        sa.Column("tenant_id",      sa.Integer(),  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("customer_id",    sa.Integer(),  sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("automation_type", sa.String(),  nullable=False),
        sa.Column("execution_id",   sa.Integer(),  sa.ForeignKey("automation_executions.id"), nullable=True),
        sa.Column("sent_at",        sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_gov_log_tenant_cust_sent",
        "governor_send_logs",
        ["tenant_id", "customer_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_gov_log_tenant_cust_sent", table_name="governor_send_logs")
    op.drop_table("governor_send_logs")
