"""Add AI generation, health, and lifecycle columns to whatsapp_templates.

Revision ID: 0009
Revises: 0008
Create Date: 2026-03-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── New columns on whatsapp_templates ────────────────────────────────────
    op.add_column("whatsapp_templates", sa.Column("source",                  sa.String(),  nullable=True, server_default="merchant"))
    op.add_column("whatsapp_templates", sa.Column("objective",               sa.String(),  nullable=True))
    op.add_column("whatsapp_templates", sa.Column("usage_count",             sa.Integer(), nullable=True, server_default="0"))
    op.add_column("whatsapp_templates", sa.Column("last_used_at",            sa.DateTime(), nullable=True))
    op.add_column("whatsapp_templates", sa.Column("health_score",            sa.Float(),   nullable=True))
    op.add_column("whatsapp_templates", sa.Column("recommendation_state",    sa.String(),  nullable=True))  # none | pending | accepted | dismissed
    op.add_column("whatsapp_templates", sa.Column("recommendation_note",     sa.Text(),    nullable=True))
    op.add_column("whatsapp_templates", sa.Column("ai_generation_metadata",  JSONB,        nullable=True))


def downgrade() -> None:
    op.drop_column("whatsapp_templates", "ai_generation_metadata")
    op.drop_column("whatsapp_templates", "recommendation_note")
    op.drop_column("whatsapp_templates", "recommendation_state")
    op.drop_column("whatsapp_templates", "health_score")
    op.drop_column("whatsapp_templates", "last_used_at")
    op.drop_column("whatsapp_templates", "usage_count")
    op.drop_column("whatsapp_templates", "objective")
    op.drop_column("whatsapp_templates", "source")
