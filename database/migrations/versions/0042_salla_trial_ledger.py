"""0042 — salla_trial_ledger: permanent per-store trial-usage ledger

Revision ID: 0042
Revises: 0041
Create Date: 2026-04-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "salla_trial_ledger",
        sa.Column("id",                     sa.Integer(),                    primary_key=True),
        sa.Column("salla_store_id",         sa.String(),  nullable=False),
        sa.Column("merchant_id",            sa.String(),  nullable=True),
        sa.Column("trial_used",             sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("first_trial_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_trial_plan",       sa.String(),  nullable=True),
        sa.Column("source",                 sa.String(),  nullable=False, server_default="salla"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("salla_store_id", name="uq_salla_trial_ledger_store_id"),
    )
    op.create_index(
        "ix_salla_trial_ledger_salla_store_id",
        "salla_trial_ledger",
        ["salla_store_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_salla_trial_ledger_salla_store_id", table_name="salla_trial_ledger")
    op.drop_table("salla_trial_ledger")
