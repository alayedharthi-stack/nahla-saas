"""Add order_confirmed and coupon_redeemed outcome columns to conversation_traces.

Revision ID: 0041
Revises: 0040

Goal:
  - outcome_tracker.py writes these columns when Salla fires order.updated
    with status=confirmed, closing the loop between "order started" in the
    AI conversation and "order confirmed" in the store.
  - Both columns are nullable so existing rows are unaffected; NULL means
    "outcome not yet observed" which is distinct from False ("not confirmed").
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_traces",
        sa.Column("order_confirmed", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.add_column(
        "conversation_traces",
        sa.Column("coupon_redeemed", sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    # Index for the outcome_tracker query: tenant + phone + order_started rows
    op.create_index(
        "ix_conversation_traces_outcome_lookup",
        "conversation_traces",
        ["tenant_id", "customer_phone", "order_started"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_traces_outcome_lookup", table_name="conversation_traces")
    op.drop_column("conversation_traces", "coupon_redeemed")
    op.drop_column("conversation_traces", "order_confirmed")
