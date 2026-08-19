"""0100 — stamp first merchant-contact email on the Customer row.

Revision ID: 0100
Revises:    0099

Adds ``customers.first_contact_notified_at``. Existing rows are backfilled
so historical customers do not generate a "new customer now" email on the
next inbound after deploy.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from migration_inspector_helpers import has_column, has_index, has_table

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

_COL = "first_contact_notified_at"
_IX = "ix_customers_tenant_first_contact_unnotified"


def upgrade() -> None:
    bind = op.get_bind()
    if not has_table(bind, "customers"):
        return
    if not has_column(bind, "customers", _COL):
        op.add_column(
            "customers",
            sa.Column(_COL, sa.DateTime(timezone=True), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE customers "
            f"SET {_COL} = COALESCE(first_seen_at, CURRENT_TIMESTAMP) "
            f"WHERE {_COL} IS NULL"
        )
    )
    if not has_index(bind, "customers", _IX):
        op.create_index(
            _IX,
            "customers",
            ["tenant_id", "id"],
            unique=False,
            postgresql_where=sa.text(f"{_COL} IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if has_index(bind, "customers", _IX):
        op.drop_index(_IX, table_name="customers")
    if has_column(bind, "customers", _COL):
        op.drop_column("customers", _COL)
