"""Add last_webhook_received_at and guardian audit log table.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── whatsapp_connections: activity tracking column ────────────────────────
    op.add_column(
        "whatsapp_connections",
        sa.Column("last_webhook_received_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── webhook_guardian_log: structured guardian audit history ───────────────
    op.create_table(
        "webhook_guardian_log",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("phone_number_id", sa.String(), nullable=True),
        sa.Column("waba_id", sa.String(), nullable=True),
        sa.Column("event", sa.String(), nullable=False),
        # webhook_subscribed | webhook_resubscribed | webhook_verification_failed
        # webhook_recovered   | webhook_stalled      | critical_error_detected
        sa.Column("success", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_webhook_guardian_log_tenant_created",
        "webhook_guardian_log",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_guardian_log_event",
        "webhook_guardian_log",
        ["event"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_guardian_log_event", table_name="webhook_guardian_log")
    op.drop_index("ix_webhook_guardian_log_tenant_created", table_name="webhook_guardian_log")
    op.drop_table("webhook_guardian_log")
    op.drop_column("whatsapp_connections", "last_webhook_received_at")
