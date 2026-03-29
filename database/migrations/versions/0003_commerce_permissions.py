"""Commerce permissions table and AIActionLog permission columns.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── commerce_permissions ──────────────────────────────────────────────────
    op.create_table(
        "commerce_permissions",
        sa.Column("id",                        sa.Integer, primary_key=True),
        sa.Column("tenant_id",                 sa.Integer, sa.ForeignKey("tenants.id"),
                  nullable=False, unique=True),
        # Default-true (safe commerce operations)
        sa.Column("can_create_orders",         sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("can_create_checkout_links", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("can_send_payment_links",    sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("can_apply_coupons",         sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        sa.Column("can_auto_generate_coupons", sa.Boolean, nullable=False,
                  server_default=sa.text("true")),
        # Default-false (opt-in only)
        sa.Column("can_cancel_orders",         sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("updated_at",                sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_commerce_permissions_tenant_id",
        "commerce_permissions",
        ["tenant_id"],
    )

    # ── Add permission_result + permission_notes to ai_action_logs ────────────
    op.add_column(
        "ai_action_logs",
        sa.Column("permission_result", sa.String, nullable=True),
    )
    op.add_column(
        "ai_action_logs",
        sa.Column("permission_notes", sa.Text, nullable=True),
    )

    # ── Add fact_guard audit columns to ai_action_logs ────────────────────────
    op.add_column(
        "ai_action_logs",
        sa.Column("fact_guard_claims", JSONB, nullable=True),
    )
    op.add_column(
        "ai_action_logs",
        sa.Column("reply_was_modified_by_fact_guard", sa.Boolean,
                  nullable=True, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("ai_action_logs", "reply_was_modified_by_fact_guard")
    op.drop_column("ai_action_logs", "fact_guard_claims")
    op.drop_column("ai_action_logs", "permission_notes")
    op.drop_column("ai_action_logs", "permission_result")

    op.drop_index("ix_commerce_permissions_tenant_id",
                  table_name="commerce_permissions")
    op.drop_table("commerce_permissions")
