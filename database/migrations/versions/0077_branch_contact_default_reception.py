"""0077 — Default reception flag on branch contacts (PR-B).

Revision ID: 0077
Revises:    0076
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "branch_contacts", "is_default_reception"):
        op.add_column(
            "branch_contacts",
            sa.Column(
                "is_default_reception",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "branch_contacts", "is_default_reception"):
        op.drop_column("branch_contacts", "is_default_reception")
