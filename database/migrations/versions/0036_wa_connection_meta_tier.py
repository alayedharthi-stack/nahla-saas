"""Add Meta messaging tier columns to whatsapp_connections

Revision ID: 0036
Revises: 0035
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("whatsapp_connections", sa.Column("meta_messaging_limit", sa.String(), nullable=True))
    op.add_column("whatsapp_connections", sa.Column("meta_quality_rating", sa.String(), nullable=True))
    op.add_column("whatsapp_connections", sa.Column("meta_tier_updated_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("whatsapp_connections", "meta_tier_updated_at")
    op.drop_column("whatsapp_connections", "meta_quality_rating")
    op.drop_column("whatsapp_connections", "meta_messaging_limit")
