"""Add connection_type column to whatsapp_connections.

Revision ID: 0016
Revises: 0015
Create Date: 2026-04-09

Adds:
  - whatsapp_connections.connection_type  ('direct' | 'embedded')
"""
from alembic import op
import sqlalchemy as sa

revision      = "0016"
down_revision = "0015"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_connections",
        sa.Column("connection_type", sa.String(), nullable=True, server_default="direct"),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "connection_type")
