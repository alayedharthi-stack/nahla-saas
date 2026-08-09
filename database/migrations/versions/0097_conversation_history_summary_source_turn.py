"""0097 — conversation_history_summaries.summary_source_turn

Tracks which conversation turn produced the stored rolling summary so
deferred background writes can apply newer-wins semantics atomically.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

_TABLE = "conversation_history_summaries"


def _has_table(bind, name: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return name in set(insp.get_table_names())
    except Exception:
        return False


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    if not _has_column(bind, _TABLE, "summary_source_turn"):
        op.add_column(
            _TABLE,
            sa.Column("summary_source_turn", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    if _has_column(bind, _TABLE, "summary_source_turn"):
        op.drop_column(_TABLE, "summary_source_turn")
