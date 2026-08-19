"""0099 — canonical Meta catalog membership (derived provider truth).

Revision ID: 0099
Revises:    0098

Creates ``meta_catalog_memberships`` as the sole authorization source for
native Meta catalog send. Grain:

    tenant_id + catalog_id + retailer_id → at most one local product/variant

Does NOT seed rows from ``products.meta_catalog_published_at``,
``external_id``, ``meta_retailer_id``, ``canonical_retailer_id``, or
``meta_last_seen_at``. The table starts empty; only a complete successful
Graph reconcile may write authoritative rows.

Does not change Product / ProductVariant identity keys.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_TABLE = "meta_catalog_memberships"
_UQ = "uq_meta_catalog_memberships_tenant_catalog_retailer"
_IX_CATALOG = "ix_meta_catalog_memberships_tenant_catalog"
_IX_LOOKUP = "ix_meta_catalog_memberships_lookup"
_IX_PRODUCT = "ix_meta_catalog_memberships_product"


def _has_table(bind, name: str) -> bool:
    try:
        return name in inspect(bind).get_table_names()
    except Exception:
        return False


def _has_index(bind, table: str, name: str) -> bool:
    if not _has_table(bind, table):
        return False
    try:
        return any(ix.get("name") == name for ix in inspect(bind).get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("catalog_id", sa.String(length=64), nullable=False),
            sa.Column("retailer_id", sa.String(length=255), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("variant_id", sa.Integer(), nullable=True),
            sa.Column("meta_item_id", sa.String(length=128), nullable=True),
            sa.Column(
                "verified_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column("provenance", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                ["tenants.id"],
                name="fk_meta_catalog_memberships_tenant",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["product_id"],
                ["products.id"],
                name="fk_meta_catalog_memberships_product",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["variant_id"],
                ["product_variants.id"],
                name="fk_meta_catalog_memberships_variant",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "catalog_id",
                "retailer_id",
                name=_UQ,
            ),
        )
    if not _has_index(bind, _TABLE, _IX_CATALOG):
        op.create_index(_IX_CATALOG, _TABLE, ["tenant_id", "catalog_id"])
    if not _has_index(bind, _TABLE, _IX_LOOKUP):
        op.create_index(
            _IX_LOOKUP, _TABLE, ["tenant_id", "catalog_id", "retailer_id"]
        )
    if not _has_index(bind, _TABLE, _IX_PRODUCT):
        op.create_index(
            _IX_PRODUCT, _TABLE, ["tenant_id", "catalog_id", "product_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, _TABLE):
        return
    for name in (_IX_PRODUCT, _IX_LOOKUP, _IX_CATALOG):
        if _has_index(bind, _TABLE, name):
            op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
