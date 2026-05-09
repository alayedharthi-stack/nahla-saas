"""0046 — sanitize any legacy ai_paused state on conversations.

Migration 0045 introduced the pause-state columns on
`conversations`. The default is `ai_paused=false` so brand-new rows are
correct, but defensive: anything already in the database with
`ai_paused=true` and either:

  * a NULL / unknown reason, or
  * no actor (`ai_paused_by` IS NULL)

is forced back to false. Rows whose reason is in the documented set
(`manual`, `human_handoff`, `bot_loop_detected`, `rate_limit`,
`internal_number`) are left untouched.

This keeps the new dashboard "AI paused" badge from sticking on
conversations that were never explicitly paused by either a merchant
or the runtime guard.

Revision ID: 0046
Revises: 0045
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


_VALID_REASONS = (
    "manual",
    "human_handoff",
    "bot_loop_detected",
    "rate_limit",
    "internal_number",
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE conversations
            SET
                ai_paused = false,
                ai_paused_reason = NULL,
                ai_paused_at = NULL,
                ai_paused_by = NULL
            WHERE
                ai_paused = true
                AND (
                    ai_paused_reason IS NULL
                    OR ai_paused_reason NOT IN :valid_reasons
                    OR ai_paused_by IS NULL
                )
            """
        ).bindparams(sa.bindparam("valid_reasons", expanding=True)),
        {"valid_reasons": list(_VALID_REASONS)},
    )


def downgrade() -> None:
    # Pure data sanitation — no schema change to revert.
    pass
