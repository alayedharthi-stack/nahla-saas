"""0085 — Product ownership fields for multi-source catalog router.

Revision ID: 0085
Revises:    0084

Adds nullable ownership / sync / conflict columns on ``products``.
No backfill — existing rows keep NULL ownership_mode until a writer sets it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

_OWNERSHIP_IDX = "ix_products_tenant_ownership_mode"
_CONFLICT_IDX = "ix_products_tenant_source_conflict_status"


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
        ("ownership_mode", sa.Column("ownership_mode", sa.String(length=32), nullable=True)),
        ("source_external_id", sa.Column("source_external_id", sa.String(length=128), nullable=True)),
        ("meta_item_id", sa.Column("meta_item_id", sa.String(length=128), nullable=True)),
        ("canonical_retailer_id", sa.Column("canonical_retailer_id", sa.String(length=255), nullable=True)),
        ("sync_status", sa.Column("sync_status", sa.String(length=32), nullable=True)),
        ("sync_error", sa.Column("sync_error", sa.Text(), nullable=True)),
        ("last_synced_at", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True)),
        ("imported_at", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True)),
        ("managed_confirmed_at", sa.Column("managed_confirmed_at", sa.DateTime(timezone=True), nullable=True)),
        ("managed_confirmed_by", sa.Column("managed_confirmed_by", sa.String(length=64), nullable=True)),
        ("source_conflict_status", sa.Column("source_conflict_status", sa.String(length=32), nullable=True)),
        (
            "source_conflict_detail",
            sa.Column("source_conflict_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        ),
    ]

    for name, col in columns:
        if not _has_column(bind, table, name):
            op.add_column(table, col)

    op.execute(
        sa.text(
            f"""
            CREATE INDEX IF NOT EXISTS {_OWNERSHIP_IDX}
            ON products (tenant_id, ownership_mode)
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE INDEX IF NOT EXISTS {_CONFLICT_IDX}
            ON products (tenant_id, source_conflict_status)
            WHERE source_conflict_status IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_CONFLICT_IDX}")
    op.execute(f"DROP INDEX IF EXISTS {_OWNERSHIP_IDX}")

    for name in (
        "source_conflict_detail",
        "source_conflict_status",
        "managed_confirmed_by",
        "managed_confirmed_at",
        "imported_at",
        "last_synced_at",
        "sync_error",
        "sync_status",
        "canonical_retailer_id",
        "meta_item_id",
        "source_external_id",
        "ownership_mode",
    ):
        bind = op.get_bind()
        if _has_column(bind, "products", name):
            op.drop_column("products", name)
