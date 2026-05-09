"""0048 — explicit human-takeover columns on conversations.

Splits the dashboard's "human reply" filter from the legacy
``status='human'`` / ``is_human_handoff`` flags. The list-conversations
endpoint now treats any of:

  * status == 'human'
  * is_human_handoff == True
  * needs_human == True
  * handoff_active == True
  * taken_over_at IS NOT NULL
  * ai_paused AND ai_paused_reason IN ('human_handoff',
    'support_escalation', 'manual_takeover')

as the unified human state. This migration adds the four new columns
that back the second half of that rule.

Backfill: any conversation already flagged as human takeover via legacy
fields receives ``needs_human = handoff_active = True`` and a
``taken_over_at = now()`` so the dashboard stays consistent on first
post-deploy load.

Revision ID: 0048
Revises: 0047
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("needs_human", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "conversations",
        sa.Column("handoff_active", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "conversations",
        sa.Column("taken_over_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("taken_over_by", sa.String(), nullable=True),
    )

    op.execute(
        """
        UPDATE conversations
        SET needs_human    = TRUE,
            handoff_active = TRUE,
            taken_over_at  = COALESCE(taken_over_at, NOW() AT TIME ZONE 'utc')
        WHERE is_human_handoff = TRUE
           OR paused_by_human  = TRUE
           OR status           = 'human'
        """
    )


def downgrade() -> None:
    op.drop_column("conversations", "taken_over_by")
    op.drop_column("conversations", "taken_over_at")
    op.drop_column("conversations", "handoff_active")
    op.drop_column("conversations", "needs_human")
