"""0097 — tenant_settings.abandoned_cart_source (Phase 1 config model).

Nullable merchant override for abandoned-cart recovery source.
NULL means use the connection-aware computed default (not a backfill).

Allowed values (enforced in application code):
  salla_storefront | nahla_shop | disabled

Does not change send/suppression runtime behavior.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

_TABLE = "tenant_settings"
_COLUMN = "abandoned_cart_source"


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
    if not _has_column(bind, _TABLE, _COLUMN):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    if _has_column(bind, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
