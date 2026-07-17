"""Add Meta messaging tier columns to whatsapp_connections

Revision ID: 0036
Revises: 0035

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
columns while ``alembic_version`` is still at 0035.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from migration_inspector_helpers import has_column

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    for column_name, column in (
        ("meta_messaging_limit", sa.Column("meta_messaging_limit", sa.String(), nullable=True)),
        ("meta_quality_rating", sa.Column("meta_quality_rating", sa.String(), nullable=True)),
        ("meta_tier_updated_at", sa.Column("meta_tier_updated_at", sa.DateTime(), nullable=True)),
    ):
        if not has_column(bind, "whatsapp_connections", column_name):
            op.add_column("whatsapp_connections", column)


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "meta_tier_updated_at")
    op.drop_column("whatsapp_connections", "meta_quality_rating")
    op.drop_column("whatsapp_connections", "meta_messaging_limit")
