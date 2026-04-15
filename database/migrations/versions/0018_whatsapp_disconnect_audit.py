"""Add disconnect audit fields to whatsapp_connections.

Revision ID: 0018
Revises: 0017
Create Date: 2026-04-15

Adds three nullable columns that are written atomically during any WhatsApp
disconnect event (merchant-initiated or admin-forced) and cleared back to NULL
when the merchant reconnects:

  disconnect_reason       VARCHAR  — 'merchant_requested_disconnect' |
                                     'admin_forced_disconnect'
  disconnected_at         TIMESTAMP — UTC timestamp of the disconnect
  disconnected_by_user_id INTEGER   — user.id of the actor (no FK; audit-only)
"""
from alembic import op
import sqlalchemy as sa


revision      = "0018"
down_revision = "0017"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_connections",
        sa.Column("disconnect_reason", sa.String(), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("disconnected_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "whatsapp_connections",
        sa.Column("disconnected_by_user_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_connections", "disconnected_by_user_id")
    op.drop_column("whatsapp_connections", "disconnected_at")
    op.drop_column("whatsapp_connections", "disconnect_reason")
