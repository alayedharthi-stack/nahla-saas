"""0049 — explicit last_read_at on conversations.

Used by the dashboard to mark a conversation as read independently of
the merchant having sent a manual reply. Until now the inbox unread
counter was "inbound messages newer than the last outbound" — which
meant simply opening a conversation never zeroed the badge until the
merchant actually replied. With ``last_read_at`` we can decouple those:

  unread = count(inbound where created_at > GREATEST(last_read_at,
                                                     last_outbound_at))

Revision ID: 0049
Revises: 0048

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
the column while ``alembic_version`` is still at 0048.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if not has_column(bind, "conversations", "last_read_at"):
        op.add_column(
            "conversations",
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("conversations", "last_read_at")
