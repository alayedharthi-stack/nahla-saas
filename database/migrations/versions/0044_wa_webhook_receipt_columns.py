"""0044 — Narrow webhook receipt columns on whatsapp_connections

Avoid rewriting huge extra_metadata JSONB on every 360dialog webhook receipt.

Revision ID: 0044
Revises: 0043
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_connections",
        sa.Column(
            "webhook_coexistence_received_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column(
            "webhook_status_received_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "webhook_status_received_at")
    op.drop_column("whatsapp_connections", "webhook_coexistence_received_at")
