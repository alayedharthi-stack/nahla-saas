"""0073 — Soft delete for merchant knowledge sections.

Revision ID: 0073
Revises:    0072

Adds ``deleted_at`` so merchant delete archives rows instead of
CASCADE-dropping media/product links. AI and default list/search
exclude soft-deleted sections.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0073"
down_revision = "0072"
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
    table = "merchant_knowledge_sections"
    if not _has_column(bind, table, "deleted_at"):
        op.add_column(
            table,
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        )
    try:
        op.create_index(
            "ix_mks_tenant_deleted_at",
            table,
            ["tenant_id", "deleted_at"],
            unique=False,
        )
    except Exception:
        pass


def downgrade() -> None:
    bind = op.get_bind()
    table = "merchant_knowledge_sections"
    try:
        op.drop_index("ix_mks_tenant_deleted_at", table_name=table)
    except Exception:
        pass
    if _has_column(bind, table, "deleted_at"):
        op.drop_column(table, "deleted_at")
