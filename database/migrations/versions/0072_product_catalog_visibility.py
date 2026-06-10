"""0072 — Product catalog visibility / Meta reconciliation state.

Revision ID: 0072
Revises:    0071

Tracks merchant-hidden and Meta-removed products without hard-deleting
rows that orders, affinities, and KB links may reference.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0072"
down_revision = "0071"
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
    table = "products"

    columns = [
        (
            "catalog_status",
            sa.Column(
                "catalog_status",
                sa.String(length=32),
                nullable=False,
                server_default="active",
            ),
        ),
        (
            "merchant_hidden_at",
            sa.Column("merchant_hidden_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "meta_last_seen_at",
            sa.Column("meta_last_seen_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "meta_removed_at",
            sa.Column("meta_removed_at", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "archived_at",
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        ),
    ]

    for name, col in columns:
        if not _has_column(bind, table, name):
            op.add_column(table, col)

    if not _has_column(bind, table, "catalog_status"):
        return

    try:
        op.create_index(
            "ix_products_tenant_catalog_status",
            table,
            ["tenant_id", "catalog_status"],
            unique=False,
        )
    except Exception:
        pass


def downgrade() -> None:
    bind = op.get_bind()
    table = "products"
    try:
        op.drop_index("ix_products_tenant_catalog_status", table_name=table)
    except Exception:
        pass
    for name in (
        "archived_at",
        "meta_removed_at",
        "meta_last_seen_at",
        "merchant_hidden_at",
        "catalog_status",
    ):
        if _has_column(bind, table, name):
            op.drop_column(table, name)
