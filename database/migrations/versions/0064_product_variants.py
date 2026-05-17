"""0064 — product_variants table + parent flags on products.

Revision ID: 0064
Revises:    0063

Why this migration exists
─────────────────────────
Nahla's catalog has been a flat one-row-per-SKU model. Every Salla
variant (size / color / material) collapses into a single
``products`` row with the variant array stuffed into the JSON column
``metadata->'variants'``. Consequences:

  * The same parent ("فستان") appears as N near-identical rows in
    search results and product cards.
  * WhatsApp / Meta sends always use the parent's retailer_id, so a
    customer who picked "size M" gets the parent card on Meta, not
    the M-specific card.
  * Google Merchant feed cannot use ``item_group_id`` because we have
    no notion of a parent → variants relationship at the SQL level.

This migration introduces the real parent / variant split:

  1. NEW ``product_variants`` table — one row per sellable SKU, FK
     back to ``products.id``, carrying per-variant retailer_id /
     price / stock / options / image / option_summary.
  2. TWO new columns on ``products`` — ``has_variants`` (Boolean) and
     ``default_variant_id`` (Integer FK → product_variants.id) so the
     sender / brain can do a cheap "give me the variant" lookup
     without a JOIN.
  3. BACKFILL — for every existing product row:
        - If ``metadata->'variants'`` is a non-empty array, insert
          one variant row per element with the parent's retailer
          fallback chain.
        - Otherwise, insert exactly ONE ``is_default=true`` synthetic
          variant mirroring the parent. This means after migration
          EVERY product has at least one variant — downstream code
          can always go "pick the variant" without legacy branching.

Idempotency
───────────
Same guarded-DDL pattern as 0061 / 0062 / 0063: every CREATE TABLE,
ADD COLUMN, CREATE INDEX is wrapped in an inspector check. The
backfill only fires for rows whose product has zero variants today
(``NOT EXISTS (SELECT 1 FROM product_variants ...)``), so re-running
this migration on a populated DB is a safe no-op.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0064"
down_revision = "0063"
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


def _has_unique_constraint(bind, table_name: str, constraint_name: str) -> bool:
    if not _has_table(bind, table_name):
        return False
    insp = inspect(bind)
    try:
        for uc in insp.get_unique_constraints(table_name):
            if uc.get("name") == constraint_name:
                return True
    except NotImplementedError:
        # SQLite — fall back to scanning unique indexes.
        for ix in insp.get_indexes(table_name):
            if ix.get("name") == constraint_name and ix.get("unique"):
                return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ── 1. product_variants table ─────────────────────────────────────────
    if not _has_table(bind, "product_variants"):
        # Use sa.JSON() so the table is portable to SQLite tests; the
        # PG migration path runs with the actual JSONB type via the
        # ORM after this DDL completes.
        json_type = sa.dialects.postgresql.JSONB() if dialect == "postgresql" else sa.JSON()
        op.create_table(
            "product_variants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(),
                      sa.ForeignKey("tenants.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer(),
                      sa.ForeignKey("products.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("salla_variant_id", sa.String(length=64),
                      nullable=True, index=True),
            sa.Column("sku", sa.String(length=128), nullable=True),
            sa.Column("retailer_id", sa.String(length=255),
                      nullable=True, index=True),
            sa.Column("price", sa.String(length=32), nullable=True),
            sa.Column("currency", sa.String(length=8), nullable=True),
            sa.Column("stock_quantity", sa.Integer(), nullable=True),
            sa.Column("in_stock", sa.Boolean(), nullable=False,
                      server_default=sa.text("true")),
            sa.Column("options", json_type, nullable=True),
            sa.Column("option_summary", sa.String(length=255), nullable=True),
            sa.Column("image_url", sa.String(length=2048), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            sa.Column("metadata", json_type, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("CURRENT_TIMESTAMP"),
                      nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.text("CURRENT_TIMESTAMP"),
                      nullable=True),
            sa.UniqueConstraint("product_id", "salla_variant_id",
                                name="uq_variants_product_salla"),
        )

    if not _has_index(bind, "product_variants",
                      "ix_variants_tenant_retailer"):
        op.create_index(
            "ix_variants_tenant_retailer",
            "product_variants",
            ["tenant_id", "retailer_id"],
            unique=False,
        )

    # ── 2. parent flags on products ───────────────────────────────────────
    if _has_table(bind, "products"):
        if not _has_column(bind, "products", "has_variants"):
            op.add_column(
                "products",
                sa.Column("has_variants", sa.Boolean(),
                          nullable=False,
                          server_default=sa.text("false")),
            )
        if not _has_column(bind, "products", "default_variant_id"):
            op.add_column(
                "products",
                sa.Column("default_variant_id", sa.Integer(),
                          sa.ForeignKey("product_variants.id"),
                          nullable=True),
            )
        if not _has_index(bind, "products",
                          "ix_products_default_variant"):
            op.create_index(
                "ix_products_default_variant",
                "products",
                ["default_variant_id"],
                unique=False,
            )

    # ── 3. backfill — every product gets at least one variant ─────────────
    # Postgres + SQLite divergent JSON access. We run two passes per
    # dialect:
    #
    #   (a) Products WITH a non-empty ``metadata->'variants'`` array
    #       get one variant row per element.
    #   (b) Products WITHOUT variants (either no metadata at all, or
    #       an empty array) get a single ``is_default=true`` synthetic
    #       variant mirroring the parent.
    #
    # Both passes are guarded by ``NOT EXISTS (SELECT 1 FROM
    # product_variants ...)`` so a re-run is a no-op.

    if dialect == "postgresql":
        # Pass (a) — variants present in metadata JSON.
        op.execute(sa.text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default, metadata
            )
            SELECT
                p.tenant_id,
                p.id,
                NULLIF(v.elem->>'id', ''),
                NULLIF(v.elem->>'sku', ''),
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, '')
                ),
                COALESCE(NULLIF(v.elem->>'price', ''), p.price),
                NULLIF(v.elem->>'currency', ''),
                NULLIF(NULLIF(v.elem->>'stock_quantity', '')::int, NULL),
                CASE
                    WHEN v.elem->>'in_stock' = 'false' THEN false
                    WHEN p.in_stock IS NOT NULL THEN p.in_stock
                    ELSE true
                END,
                CASE
                    WHEN jsonb_typeof(v.elem->'options') = 'object'
                        THEN v.elem->'options'
                    ELSE NULL
                END,
                NULLIF(v.elem->>'option_summary', ''),
                NULLIF(v.elem->>'image_url', ''),
                false,
                v.elem
            FROM products p
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(p.metadata::jsonb->'variants') = 'array'
                        THEN p.metadata::jsonb->'variants'
                    ELSE '[]'::jsonb
                END
            ) AS v(elem)
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv
                WHERE pv.product_id = p.id
            )
        """))

        # Pass (b) — products with NO variants get a synthetic default
        # row mirroring the parent. ``salla_variant_id`` is NULL so the
        # unique constraint allows future inserts to coexist.
        op.execute(sa.text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default
            )
            SELECT
                p.tenant_id, p.id, NULL, p.sku,
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, ''),
                    'nahla_p_' || p.id
                ),
                p.price, NULL, p.stock_quantity,
                COALESCE(p.in_stock, true),
                NULL, NULL, NULL,
                true
            FROM products p
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv WHERE pv.product_id = p.id
            )
        """))

        # ── Stamp parent flags ──
        # ``has_variants`` true when 2+ variant rows OR a single non-
        # default variant (Salla product with exactly one option group
        # currently selected).
        op.execute(sa.text("""
            UPDATE products p
               SET has_variants = (
                   SELECT COUNT(*) > 1 OR BOOL_OR(NOT pv.is_default)
                     FROM product_variants pv
                    WHERE pv.product_id = p.id
               )
        """))

        # ``default_variant_id`` → the ``is_default=true`` row if one
        # exists, else the lowest-id row (deterministic).
        op.execute(sa.text("""
            UPDATE products p
               SET default_variant_id = (
                   SELECT pv.id FROM product_variants pv
                    WHERE pv.product_id = p.id
                 ORDER BY pv.is_default DESC, pv.id ASC
                    LIMIT 1
               )
             WHERE default_variant_id IS NULL
        """))

    elif dialect == "sqlite":
        # SQLite stores JSONB as TEXT; use json_each / json_extract.
        # We assume the loaded sqlite3 build exposes the JSON1
        # extension (every Python 3.13 build on the CI matrix does).
        op.execute(sa.text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default, metadata
            )
            SELECT
                p.tenant_id,
                p.id,
                NULLIF(json_extract(v.value, '$.id'), ''),
                NULLIF(json_extract(v.value, '$.sku'), ''),
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, '')
                ),
                COALESCE(NULLIF(json_extract(v.value, '$.price'), ''), p.price),
                NULLIF(json_extract(v.value, '$.currency'), ''),
                CAST(NULLIF(json_extract(v.value, '$.stock_quantity'), '') AS INTEGER),
                CASE
                    WHEN json_extract(v.value, '$.in_stock') = 0 THEN 0
                    WHEN p.in_stock IS NOT NULL THEN p.in_stock
                    ELSE 1
                END,
                CASE
                    WHEN json_type(v.value, '$.options') = 'object'
                        THEN json_extract(v.value, '$.options')
                    ELSE NULL
                END,
                NULLIF(json_extract(v.value, '$.option_summary'), ''),
                NULLIF(json_extract(v.value, '$.image_url'), ''),
                0,
                v.value
            FROM products p, json_each(
                CASE
                    WHEN json_type(p.metadata, '$.variants') = 'array'
                        THEN json_extract(p.metadata, '$.variants')
                    ELSE '[]'
                END
            ) AS v
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv
                WHERE pv.product_id = p.id
            )
        """))

        op.execute(sa.text("""
            INSERT INTO product_variants (
                tenant_id, product_id, salla_variant_id, sku,
                retailer_id, price, currency, stock_quantity,
                in_stock, options, option_summary, image_url,
                is_default
            )
            SELECT
                p.tenant_id, p.id, NULL, p.sku,
                COALESCE(
                    NULLIF(p.meta_retailer_id, ''),
                    NULLIF(p.external_id, ''),
                    'nahla_p_' || p.id
                ),
                p.price, NULL, p.stock_quantity,
                COALESCE(p.in_stock, 1),
                NULL, NULL, NULL,
                1
            FROM products p
            WHERE NOT EXISTS (
                SELECT 1 FROM product_variants pv WHERE pv.product_id = p.id
            )
        """))

        # SQLite doesn't have BOOL_OR; emulate with MAX over an int.
        op.execute(sa.text("""
            UPDATE products
               SET has_variants = (
                   SELECT
                       CASE
                           WHEN COUNT(*) > 1 THEN 1
                           WHEN MAX(CASE WHEN pv.is_default = 0 THEN 1 ELSE 0 END) = 1 THEN 1
                           ELSE 0
                       END
                     FROM product_variants pv
                    WHERE pv.product_id = products.id
               )
        """))

        op.execute(sa.text("""
            UPDATE products
               SET default_variant_id = (
                   SELECT pv.id FROM product_variants pv
                    WHERE pv.product_id = products.id
                 ORDER BY pv.is_default DESC, pv.id ASC
                    LIMIT 1
               )
             WHERE default_variant_id IS NULL
        """))


def downgrade() -> None:
    bind = op.get_bind()

    if _has_index(bind, "products", "ix_products_default_variant"):
        op.drop_index("ix_products_default_variant", "products")
    if _has_column(bind, "products", "default_variant_id"):
        op.drop_column("products", "default_variant_id")
    if _has_column(bind, "products", "has_variants"):
        op.drop_column("products", "has_variants")

    if _has_index(bind, "product_variants", "ix_variants_tenant_retailer"):
        op.drop_index("ix_variants_tenant_retailer", "product_variants")
    if _has_table(bind, "product_variants"):
        op.drop_table("product_variants")
