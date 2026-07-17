"""Add order_confirmed and coupon_redeemed outcome columns to conversation_traces.

Revision ID: 0041
Revises: 0040

Goal:
  - outcome_tracker.py writes these columns when Salla fires order.updated
    with status=confirmed, closing the loop between "order started" in the
    AI conversation and "order confirmed" in the store.
  - Both columns are nullable so existing rows are unaffected; NULL means
    "outcome not yet observed" which is distinct from False ("not confirmed").

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
columns or the outcome lookup index while ``alembic_version`` is still
at 0040.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column, has_index

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if not has_column(bind, "conversation_traces", "order_confirmed"):
        op.add_column(
            "conversation_traces",
            sa.Column("order_confirmed", sa.Boolean(), nullable=True, server_default=sa.false()),
        )
    if not has_column(bind, "conversation_traces", "coupon_redeemed"):
        op.add_column(
            "conversation_traces",
            sa.Column("coupon_redeemed", sa.Boolean(), nullable=True, server_default=sa.false()),
        )

    if not has_index(bind, "conversation_traces", "ix_conversation_traces_outcome_lookup"):
        op.create_index(
            "ix_conversation_traces_outcome_lookup",
            "conversation_traces",
            ["tenant_id", "customer_phone", "order_started"],
        )


def downgrade() -> None:
    op.drop_index("ix_conversation_traces_outcome_lookup", table_name="conversation_traces")
    op.drop_column("conversation_traces", "coupon_redeemed")
    op.drop_column("conversation_traces", "order_confirmed")
