"""0071 — Meta catalog import diagnostics columns on whatsapp_connections.

Revision ID: 0071
Revises:    0070

Why this migration exists
─────────────────────────
PR2 — operational visibility for Meta catalog imports. Merchants and
support need to see the last import run (status, timestamp, counts,
token source) without digging through Railway logs. All columns are
nullable — existing rows stay untouched until the next import.

Idempotency
───────────
Follows the 0061 / 0070 pattern: every column add is guarded by an
inspector check so re-running on a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c.get("name") == column for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    table = "whatsapp_connections"

    columns = [
        ("meta_import_status", sa.Column("meta_import_status", sa.String(length=32), nullable=True)),
        (
            "meta_import_last_at",
            sa.Column("meta_import_last_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "meta_import_last_error",
            sa.Column("meta_import_last_error", sa.Text(), nullable=True),
        ),
        (
            "meta_import_last_report",
            sa.Column("meta_import_last_report", JSONB(), nullable=True),
        ),
        (
            "meta_import_token_source",
            sa.Column("meta_import_token_source", sa.String(length=64), nullable=True),
        ),
    ]

    for name, col in columns:
        if not _has_column(bind, table, name):
            op.add_column(table, col)


def downgrade() -> None:
    bind = op.get_bind()
    table = "whatsapp_connections"
    for name in (
        "meta_import_token_source",
        "meta_import_last_report",
        "meta_import_last_error",
        "meta_import_last_at",
        "meta_import_status",
    ):
        if _has_column(bind, table, name):
            op.drop_column(table, name)
