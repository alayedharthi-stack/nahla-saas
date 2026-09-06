"""0105 — last_customer_inbound_at on wa_conversation_windows.

Revision ID: 0105
Revises:    0104

Dedicated customer-service window truth, physically independent from the
billing ``window_start`` clock. Nullable: missing inbound stays CLOSED.

Do not use ``alembic upgrade head``. Apply with ``alembic upgrade 0105``.
This revision does not apply itself to Production.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column, has_table

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

_TABLE = "wa_conversation_windows"
_COLUMN = "last_customer_inbound_at"


def upgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return
    if has_column(bind, _TABLE, _COLUMN):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, _TABLE):
        return
    if has_column(bind, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
