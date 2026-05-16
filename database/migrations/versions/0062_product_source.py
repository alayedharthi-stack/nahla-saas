"""0062 — Product.source column for catalog source-agnostic architecture.

Revision ID: 0062
Revises:    0061

Why this migration exists
─────────────────────────
The Catalog feature in Nahla is a first-class asset that multiple
channels consume (WhatsApp / Meta / future campaigns / future
checkout). Products themselves can come from many sources:

  • Salla store sync (existing)
  • Zid store sync (existing)
  • Manual entry from the Nahla dashboard (new, this PR)
  • Future: Shopify, WooCommerce, CSV upload

Until now, "which source produced this row?" was buried inside
``Product.extra_metadata->>'source'`` for Salla-synced rows only,
and entirely absent for everything else. The dashboard couldn't
render a "data source" badge without scanning JSONB on every row,
and per-source resync / purge endpoints had no clean filter.

This migration adds a stable top-level ``source`` column on
``products``, indexed for cheap GROUP BY queries (diagnostics
endpoint), and backfills existing rows with the best signal we
have:

  • ``extra_metadata.source = 'salla'``  → ``source = 'salla'``
  • ``extra_metadata.source = 'zid'``    → ``source = 'zid'``
  • Otherwise — if the row has an ``external_id``, assume the
    most likely existing source: ``salla`` (the Salla sync ran
    on the vast majority of production deploys). Mixed-platform
    tenants will get a one-time `/admin/catalog/relabel-source`
    call later. We deliberately do NOT default to ``manual``
    here because that string carries a hard contract: "no
    upstream source can ever overwrite this row on a sync run".

Idempotency
───────────
Same pattern as 0058 / 0059 / 0061: every column add + index add
+ backfill is wrapped in an inspector check so re-running the
migration on a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0062"
down_revision = "0061"
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
    dialect = bind.dialect.name

    if not _has_column(bind, "products", "source"):
        op.add_column(
            "products",
            sa.Column("source", sa.String(length=32), nullable=True),
        )

    if not _has_index(bind, "products", "ix_products_source"):
        op.create_index(
            "ix_products_source", "products", ["source"], unique=False,
        )

    # Backfill — only on Postgres / SQLite (i.e. anything we ship to).
    # We intentionally run this once at upgrade time so freshly-added
    # diagnostics report meaningful numbers on day one. Subsequent
    # writes go through the application layer (Salla sync, manual
    # CRUD, etc.) which set the column directly.
    #
    # Column-naming note (May 2026 #19c — production fix):
    # ``Product.extra_metadata = Column('metadata', JSONB)`` in
    # ``database/models.py:154`` — i.e. the ORM ATTRIBUTE is
    # ``extra_metadata`` but the actual Postgres COLUMN is named
    # ``metadata``. The original SQL here referenced
    # ``extra_metadata`` and blew up on production with
    # ``UndefinedColumn: column "extra_metadata" does not exist``,
    # leaving the migration rolled back at 0061. Use the real DB
    # column name in raw SQL (the SQLite branch below already
    # used ``metadata`` correctly).
    if dialect == "postgresql":
        op.execute(sa.text("""
            UPDATE products
               SET source = COALESCE(
                       NULLIF(metadata::jsonb->>'source', ''),
                       metadata::jsonb->>'source',
                       CASE
                           WHEN external_id IS NOT NULL
                                AND external_id <> ''
                           THEN 'salla'
                           ELSE 'unknown'
                       END
                   )
             WHERE source IS NULL
        """))
    elif dialect == "sqlite":
        # SQLite stores JSONB as TEXT — use json_extract.
        op.execute(sa.text("""
            UPDATE products
               SET source = COALESCE(
                       json_extract(metadata, '$.source'),
                       CASE
                           WHEN external_id IS NOT NULL
                                AND external_id <> ''
                           THEN 'salla'
                           ELSE 'unknown'
                       END
                   )
             WHERE source IS NULL
        """))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "products", "ix_products_source"):
        op.drop_index("ix_products_source", "products")
    if _has_column(bind, "products", "source"):
        op.drop_column("products", "source")
