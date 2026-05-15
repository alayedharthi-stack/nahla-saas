"""0061 — Meta WhatsApp Catalog columns.

Revision ID: 0061
Revises:    0060

Why this migration exists
─────────────────────────
Phase 2 of the Catalog Commerce feature. We need to send real Meta
WhatsApp Catalog messages (``interactive.type = "product"`` and
``"product_list"``) instead of the legacy image + CTA-URL pair. That
requires two identifiers we don't store today:

* The merchant's **Meta Catalog id** — every catalog message payload
  carries it inside ``action.catalog_id``. It's per-WABA so we store it
  on ``whatsapp_connections``.
* The product's **Meta retailer id** — the id under which the product
  is published in Meta Commerce Manager (matches ``action
  .product_retailer_id`` in the message payload). For most merchants
  using Salla auto-publish to Meta this is identical to
  ``Product.external_id`` — so the runtime helper
  (``effective_retailer_id``) defaults to ``external_id`` whenever
  ``meta_retailer_id`` is NULL. The column exists for tenants who
  publish products to Meta manually with different ids.

Per-tenant kill-switch (``catalog_enabled``) lives next to
``meta_catalog_id`` so we can flip catalog sending OFF without
nuking the catalog id when a tenant pauses the feature.

Idempotency
───────────
Follows the 0056-0060 pattern: every column add is guarded by an
inspector check so re-running on a populated DB is a safe no-op. The
unique index on ``(tenant_id, meta_retailer_id)`` only fires when the
``products`` table actually exists.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def _has_column(bind, table_name: str, column_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        c["name"] == column_name
        for c in inspect(bind).get_columns(table_name)
    )


def _has_index(bind, table_name: str, index_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    return any(
        ix["name"] == index_name
        for ix in inspect(bind).get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    # ── WhatsApp connections — catalog identity + kill-switch ─────────────
    if _has_table(bind, "whatsapp_connections"):
        if not _has_column(bind, "whatsapp_connections", "meta_catalog_id"):
            op.add_column(
                "whatsapp_connections",
                sa.Column("meta_catalog_id", sa.String(length=255), nullable=True),
            )
        if not _has_column(bind, "whatsapp_connections", "catalog_enabled"):
            op.add_column(
                "whatsapp_connections",
                sa.Column(
                    "catalog_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

    # ── Products — Meta retailer id override + publish timestamp ──────────
    if _has_table(bind, "products"):
        if not _has_column(bind, "products", "meta_retailer_id"):
            op.add_column(
                "products",
                sa.Column("meta_retailer_id", sa.String(length=255), nullable=True),
            )
        if not _has_column(bind, "products", "meta_catalog_published_at"):
            op.add_column(
                "products",
                sa.Column(
                    "meta_catalog_published_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                ),
            )

        # Composite index — lookup retailer_id within a tenant scope. NOT
        # UNIQUE because (a) a merchant might intentionally publish two
        # local variants under the same retailer_id while migrating, and
        # (b) NULL meta_retailer_id values are valid and must be allowed
        # to repeat freely.
        if not _has_index(
            bind, "products", "ix_products_tenant_retailer"
        ):
            op.create_index(
                "ix_products_tenant_retailer",
                "products",
                ["tenant_id", "meta_retailer_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_index(bind, "products", "ix_products_tenant_retailer"):
        op.drop_index("ix_products_tenant_retailer", "products")

    if _has_column(bind, "products", "meta_catalog_published_at"):
        op.drop_column("products", "meta_catalog_published_at")
    if _has_column(bind, "products", "meta_retailer_id"):
        op.drop_column("products", "meta_retailer_id")

    if _has_column(bind, "whatsapp_connections", "catalog_enabled"):
        op.drop_column("whatsapp_connections", "catalog_enabled")
    if _has_column(bind, "whatsapp_connections", "meta_catalog_id"):
        op.drop_column("whatsapp_connections", "meta_catalog_id")
