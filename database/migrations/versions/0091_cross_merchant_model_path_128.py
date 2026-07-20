"""0091 — widen cross_merchant_signals.model_path (integration-bootstrap branch).

Sibling to ``0090`` on the ``0088`` A1-Validate branch. Normal bootstrap pins to
this revision so integration environments pick up the widen without ``head``.

Does not rewrite historical ``0033`` — forward-only column widen.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_table

revision = "0091"
down_revision = "0089"
branch_labels = None
depends_on = None

_TABLE = "cross_merchant_signals"
_COLUMN = "model_path"
_TARGET_LENGTH = 128


def _varchar_length(bind, table: str, column: str) -> int | None:
    insp = sa.inspect(bind)
    try:
        for col in insp.get_columns(table):
            if col.get("name") == column:
                return getattr(col.get("type"), "length", None)
    except Exception:
        return None
    return None


def upgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return

    current = _varchar_length(bind, _TABLE, _COLUMN)
    if current is not None and current >= _TARGET_LENGTH:
        return

    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=current or 32),
        type_=sa.String(length=_TARGET_LENGTH),
        existing_nullable=False,
        existing_server_default="rule",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return

    current = _varchar_length(bind, _TABLE, _COLUMN)
    if current is not None and current <= 32:
        return

    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(length=current or _TARGET_LENGTH),
        type_=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="rule",
    )
