"""0045 — AI pause state on conversations + tenant blocked numbers

Adds the loop-guard state needed by core/ai_pause_guard:

* `conversations.ai_paused`           — boolean, defaults false
* `conversations.ai_paused_reason`    — manual | human_handoff |
                                        bot_loop_detected | rate_limit |
                                        internal_number
* `conversations.ai_paused_at`        — timestamp the pause flipped on
* `conversations.ai_paused_by`        — user id or 'system:<reason>'
* `tenants.ai_blocked_numbers`        — JSONB array of normalized phones
                                        whose inbound messages should never
                                        reach the LLM

Revision ID: 0045
Revises: 0044

Idempotency (F16)
─────────────────
Guarded by inspector checks — safe when forward-ORM drift pre-created
columns while ``alembic_version`` is still at 0044.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from migration_inspector_helpers import has_column

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    for column_name, column in (
        (
            "ai_paused",
            sa.Column("ai_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        ),
        ("ai_paused_reason", sa.Column("ai_paused_reason", sa.String(), nullable=True)),
        (
            "ai_paused_at",
            sa.Column("ai_paused_at", sa.DateTime(timezone=True), nullable=True),
        ),
        ("ai_paused_by", sa.Column("ai_paused_by", sa.String(), nullable=True)),
    ):
        if not has_column(bind, "conversations", column_name):
            op.add_column("conversations", column)

    if not has_column(bind, "tenants", "ai_blocked_numbers"):
        op.add_column(
            "tenants",
            sa.Column(
                "ai_blocked_numbers",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("tenants", "ai_blocked_numbers")
    op.drop_column("conversations", "ai_paused_by")
    op.drop_column("conversations", "ai_paused_at")
    op.drop_column("conversations", "ai_paused_reason")
    op.drop_column("conversations", "ai_paused")
