"""Dormant Shopify catalog mirror tables (read-only import, flag off).

Revision ID: 0122
Revises: 0121

Creates the seven Shopify catalog tables declared on ``ShopifyCatalogBase`` in
``backend/services/shopify_catalog/models.py`` (not part of ``models.Base``,
so startup ``create_all`` never builds them) and the ``(id, tenant_id)``
unique key on ``shopify_connections`` that their composite foreign keys use:

  shopify_catalog_import_runs        one row per import attempt; at most one
                                     running per connection (partial unique
                                     index); commit evidence CHECK
  shopify_catalog_staged_records     validated records of a running import
  shopify_catalog_products,          provider-scoped mirror keyed by
  shopify_catalog_inventory_items,   (connection, Shopify GID) with
  shopify_catalog_variants,          tenant-carrying composite FKs;
  shopify_catalog_locations,         inventory levels are item-owned
  shopify_catalog_inventory_levels   (item, location)

Nothing is written to ``products`` / ``product_variants`` or any other
existing table; the only change to an existing table is the additive
``uq_shopify_connections_id_tenant`` key (trivially satisfied: ``id`` is
already the primary key).

Fail closed:
  * the foundation tables of revision 0121 must exist with
    ``shopify_connections(id, tenant_id)``, else the upgrade refuses;
  * if any catalog table or the connection key already exists — complete,
    partial or malformed — the upgrade refuses and changes nothing: they are
    new here, so a pre-existing one was not built by this revision.

Chained on 0121 and replaces it as that branch's head; normal bootstrap
(pinned to 0093) never applies it. Apply only as an explicit, owner-approved
step. Downgrade drops only these seven tables and the key.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

CONNECTIONS = "shopify_connections"
CONNECTION_KEY = "uq_shopify_connections_id_tenant"
RUNS = "shopify_catalog_import_runs"
STAGED = "shopify_catalog_staged_records"
PRODUCTS = "shopify_catalog_products"
ITEMS = "shopify_catalog_inventory_items"
VARIANTS = "shopify_catalog_variants"
LOCATIONS = "shopify_catalog_locations"
LEVELS = "shopify_catalog_inventory_levels"
TABLES = (RUNS, STAGED, PRODUCTS, ITEMS, VARIANTS, LOCATIONS, LEVELS)
FOUNDATION_TABLES = ("shopify_connections", "shopify_shop_leases", "shopify_oauth_states",
                     "shopify_webhook_events", "shopify_connection_audit")

_GID = "[1-9][0-9]{0,19}"


def _ts(name: str, nullable: bool = True, default: bool = False) -> sa.Column:
    kwargs = {"server_default": sa.text("now()")} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kwargs)


def _mirror(table: str, removed: str) -> list:
    return [
        sa.Column("lifecycle", sa.String(16), nullable=False),
        _ts("removed_at"),
        sa.Column("first_seen_run_id", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_run_id", sa.BigInteger(), nullable=False),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
        sa.ForeignKeyConstraint(["first_seen_run_id", "connection_id"], [f"{RUNS}.id", f"{RUNS}.connection_id"],
                                name=f"fk_{table}_first_run"),
        sa.ForeignKeyConstraint(["last_seen_run_id", "connection_id"], [f"{RUNS}.id", f"{RUNS}.connection_id"],
                                name=f"fk_{table}_last_run"),
        sa.CheckConstraint(
            f"(lifecycle = 'present' AND removed_at IS NULL) OR (lifecycle = '{removed}' AND removed_at IS NOT NULL)",
            name=f"ck_{table}_lifecycle",
        ),
    ]


def _connection_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["connection_id", "tenant_id"], [f"{CONNECTIONS}.id", f"{CONNECTIONS}.tenant_id"],
                                   name=f"fk_{table}_connection")


def _definitions() -> dict:
    meta = sa.MetaData()
    sa.Table("tenants", meta, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(CONNECTIONS, meta, sa.Column("id", sa.BigInteger, primary_key=True),
             sa.Column("tenant_id", sa.Integer, nullable=False),
             sa.UniqueConstraint("id", "tenant_id", name=CONNECTION_KEY))
    runs = sa.Table(
        RUNS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("run_seq", sa.BigInteger(), nullable=False),
        sa.Column("shop_domain", sa.String(255), nullable=False),
        sa.Column("shop_gid", sa.String(64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("api_version", sa.String(7), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.String(48), nullable=True),
        sa.Column("inventory_mode", sa.String(24), nullable=True),
        sa.Column("lease_id", sa.String(64), nullable=True),
        _ts("lease_expires_at"),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("shop_currency", sa.String(3), nullable=True),
        sa.Column("expected_products", sa.Integer(), nullable=True),
        sa.Column("traversal_complete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pages_fetched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("products_count", sa.Integer(), nullable=True),
        sa.Column("variants_count", sa.Integer(), nullable=True),
        sa.Column("inventory_items_count", sa.Integer(), nullable=True),
        sa.Column("locations_count", sa.Integer(), nullable=True),
        sa.Column("inventory_levels_count", sa.Integer(), nullable=True),
        sa.Column("products_removed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("variants_removed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stale_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("started_at", nullable=False, default=True),
        _ts("heartbeat_at"),
        _ts("finished_at"),
        _ts("committed_at"),
        _connection_fk(RUNS),
        sa.UniqueConstraint("connection_id", "run_seq", name="uq_shopify_catalog_runs_seq"),
        sa.UniqueConstraint("id", "connection_id", name="uq_shopify_catalog_runs_id_connection"),
        sa.CheckConstraint("status IN ('running', 'committed', 'failed', 'cancelled')",
                           name="ck_shopify_catalog_runs_status"),
        sa.CheckConstraint("inventory_mode IS NULL OR inventory_mode IN ('per_location', 'scope_missing')",
                           name="ck_shopify_catalog_runs_inventory_mode"),
        sa.CheckConstraint("api_version ~ '^20[2-9][0-9]-(01|04|07|10)$'", name="ck_shopify_catalog_runs_api_version"),
        sa.CheckConstraint(f"shop_gid ~ '^gid://shopify/Shop/{_GID}$'", name="ck_shopify_catalog_runs_shop_gid"),
        sa.CheckConstraint("run_seq >= 1 AND generation >= 1", name="ck_shopify_catalog_runs_versions"),
        sa.CheckConstraint(
            "(status = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND finished_at IS NULL AND failure_reason IS NULL AND committed_at IS NULL)"
            " OR (status <> 'running' AND lease_id IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL)",
            name="ck_shopify_catalog_runs_lease",
        ),
        sa.CheckConstraint(
            "(status IN ('failed', 'cancelled') AND failure_reason IS NOT NULL AND committed_at IS NULL)"
            " OR status IN ('running', 'committed')",
            name="ck_shopify_catalog_runs_failure",
        ),
        sa.CheckConstraint(
            "status <> 'committed' OR (committed_at IS NOT NULL AND failure_reason IS NULL"
            " AND traversal_complete AND shop_currency IS NOT NULL AND inventory_mode IS NOT NULL"
            " AND products_count IS NOT NULL AND variants_count IS NOT NULL"
            " AND inventory_items_count IS NOT NULL"
            " AND ((inventory_mode = 'per_location' AND locations_count IS NOT NULL"
            "       AND inventory_levels_count IS NOT NULL)"
            "   OR (inventory_mode = 'scope_missing' AND locations_count IS NULL"
            "       AND inventory_levels_count IS NULL)))",
            name="ck_shopify_catalog_runs_commit_evidence",
        ),
        sa.CheckConstraint(
            "coalesce(products_count, 0) >= 0 AND coalesce(variants_count, 0) >= 0"
            " AND coalesce(inventory_items_count, 0) >= 0 AND coalesce(locations_count, 0) >= 0"
            " AND coalesce(inventory_levels_count, 0) >= 0 AND pages_fetched >= 0"
            " AND products_removed >= 0 AND variants_removed >= 0 AND stale_skipped >= 0",
            name="ck_shopify_catalog_runs_counts",
        ),
        sa.Index("uq_shopify_catalog_runs_one_running", "connection_id", unique=True,
                 postgresql_where=sa.text("status = 'running'")),
        sa.Index("ix_shopify_catalog_runs_tenant_connection", "tenant_id", "connection_id", "run_seq"),
    )
    staged = sa.Table(
        STAGED, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey(f"{RUNS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("gid", sa.String(128), nullable=False),
        sa.Column("parent_gid", sa.String(128), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("run_id", "kind", "gid", name="uq_shopify_catalog_staged_identity"),
        sa.CheckConstraint(
            "kind IN ('product', 'variant', 'inventory_item', 'location', 'inventory_level')",
            name="ck_shopify_catalog_staged_kind",
        ),
    )
    products = sa.Table(
        PRODUCTS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("product_gid", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("handle", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("vendor", sa.String(255), nullable=True),
        sa.Column("product_type", sa.String(255), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False),
        sa.Column("image_url", sa.String(2048), nullable=True),
        sa.Column("image_alt", sa.String(512), nullable=True),
        sa.Column("images", postgresql.JSONB(), nullable=False),
        _ts("online_store_published_at"),
        sa.Column("is_gift_card", sa.Boolean(), nullable=False),
        sa.Column("requires_selling_plan", sa.Boolean(), nullable=False),
        sa.Column("combined_listing_role", sa.String(16), nullable=True),
        sa.Column("has_only_default_variant", sa.Boolean(), nullable=False),
        sa.Column("variants_count", sa.Integer(), nullable=False),
        _ts("source_created_at", nullable=False),
        _ts("source_updated_at", nullable=False),
        *_mirror(PRODUCTS, "deleted"),
        _connection_fk(PRODUCTS),
        sa.UniqueConstraint("connection_id", "product_gid", name="uq_shopify_catalog_products_identity"),
        sa.UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_products_parent_key"),
        sa.CheckConstraint(f"product_gid ~ '^gid://shopify/Product/{_GID}$'", name="ck_shopify_catalog_products_gid"),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED', 'DRAFT', 'UNLISTED')",
                           name="ck_shopify_catalog_products_status"),
        sa.CheckConstraint("combined_listing_role IS NULL OR combined_listing_role IN ('PARENT', 'CHILD')",
                           name="ck_shopify_catalog_products_combined_role"),
        sa.CheckConstraint("variants_count >= 0", name="ck_shopify_catalog_products_variants_count"),
        sa.Index("ix_shopify_catalog_products_tenant", "tenant_id", "connection_id", "lifecycle"),
    )
    items = sa.Table(
        ITEMS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("inventory_item_gid", sa.String(64), nullable=False),
        sa.Column("tracking", sa.String(10), nullable=False),
        _ts("source_updated_at", nullable=False),
        *_mirror(ITEMS, "detached"),
        _connection_fk(ITEMS),
        sa.UniqueConstraint("connection_id", "inventory_item_gid", name="uq_shopify_catalog_items_identity"),
        sa.UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_items_parent_key"),
        sa.CheckConstraint(f"inventory_item_gid ~ '^gid://shopify/InventoryItem/{_GID}$'",
                           name="ck_shopify_catalog_items_gid"),
        sa.CheckConstraint("tracking IN ('tracked', 'untracked')", name="ck_shopify_catalog_items_tracking"),
    )
    variants = sa.Table(
        VARIANTS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("inventory_item_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_gid", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("sku", sa.String(255), nullable=True),
        sa.Column("barcode", sa.String(255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("compare_at_price", sa.Numeric(), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("selected_options", postgresql.JSONB(), nullable=False),
        sa.Column("image_url", sa.String(2048), nullable=True),
        sa.Column("image_alt", sa.String(512), nullable=True),
        sa.Column("image_source", sa.String(8), nullable=True),
        sa.Column("inventory_policy", sa.String(8), nullable=False),
        sa.Column("available_for_sale", sa.Boolean(), nullable=False),
        sa.Column("requires_components", sa.Boolean(), nullable=False),
        _ts("source_created_at", nullable=False),
        _ts("source_updated_at", nullable=False),
        *_mirror(VARIANTS, "deleted"),
        sa.ForeignKeyConstraint(["product_id", "connection_id", "tenant_id"],
                                [f"{PRODUCTS}.id", f"{PRODUCTS}.connection_id", f"{PRODUCTS}.tenant_id"],
                                name="fk_shopify_catalog_variants_product"),
        sa.ForeignKeyConstraint(["inventory_item_id", "connection_id", "tenant_id"],
                                [f"{ITEMS}.id", f"{ITEMS}.connection_id", f"{ITEMS}.tenant_id"],
                                name="fk_shopify_catalog_variants_item"),
        sa.UniqueConstraint("connection_id", "variant_gid", name="uq_shopify_catalog_variants_identity"),
        sa.CheckConstraint(f"variant_gid ~ '^gid://shopify/ProductVariant/{_GID}$'",
                           name="ck_shopify_catalog_variants_gid"),
        sa.CheckConstraint("inventory_policy IN ('DENY', 'CONTINUE')", name="ck_shopify_catalog_variants_inventory_policy"),
        sa.CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="ck_shopify_catalog_variants_currency"),
        sa.CheckConstraint("price >= 0 AND (compare_at_price IS NULL OR compare_at_price >= 0)",
                           name="ck_shopify_catalog_variants_price"),
        sa.CheckConstraint(
            "(image_url IS NULL AND image_source IS NULL AND image_alt IS NULL)"
            " OR (image_url IS NOT NULL AND image_source IN ('variant', 'product'))",
            name="ck_shopify_catalog_variants_image",
        ),
        sa.Index("ix_shopify_catalog_variants_product", "product_id"),
        sa.Index("ix_shopify_catalog_variants_item", "inventory_item_id"),
    )
    locations = sa.Table(
        LOCATIONS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("location_gid", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("fulfills_online_orders", sa.Boolean(), nullable=False),
        *_mirror(LOCATIONS, "deleted"),
        _connection_fk(LOCATIONS),
        sa.UniqueConstraint("connection_id", "location_gid", name="uq_shopify_catalog_locations_identity"),
        sa.UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_locations_parent_key"),
        sa.CheckConstraint(f"location_gid ~ '^gid://shopify/Location/{_GID}$'", name="ck_shopify_catalog_locations_gid"),
    )
    levels = sa.Table(
        LEVELS, meta,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("inventory_item_id", sa.BigInteger(), nullable=False),
        sa.Column("location_id", sa.BigInteger(), nullable=False),
        sa.Column("inventory_level_gid", sa.String(128), nullable=False),
        sa.Column("available", sa.Integer(), nullable=False),
        _ts("source_updated_at", nullable=False),
        *_mirror(LEVELS, "detached"),
        sa.ForeignKeyConstraint(["inventory_item_id", "connection_id", "tenant_id"],
                                [f"{ITEMS}.id", f"{ITEMS}.connection_id", f"{ITEMS}.tenant_id"],
                                name="fk_shopify_catalog_levels_item"),
        sa.ForeignKeyConstraint(["location_id", "connection_id", "tenant_id"],
                                [f"{LOCATIONS}.id", f"{LOCATIONS}.connection_id", f"{LOCATIONS}.tenant_id"],
                                name="fk_shopify_catalog_levels_location"),
        sa.UniqueConstraint("inventory_item_id", "location_id", name="uq_shopify_catalog_levels_item_location"),
        sa.UniqueConstraint("connection_id", "inventory_level_gid", name="uq_shopify_catalog_levels_identity"),
        sa.CheckConstraint(
            f"inventory_level_gid ~ '^gid://shopify/InventoryLevel/{_GID}\\?inventory_item_id={_GID}$'",
            name="ck_shopify_catalog_levels_gid",
        ),
    )
    return {RUNS: runs, STAGED: staged, PRODUCTS: products, ITEMS: items, VARIANTS: variants,
            LOCATIONS: locations, LEVELS: levels}


def _connection_key_present(inspector) -> bool:
    return any(c.get("name") == CONNECTION_KEY for c in inspector.get_unique_constraints(CONNECTIONS))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    names = set(inspector.get_table_names())
    missing = sorted(set(FOUNDATION_TABLES) - names)
    if missing:
        raise RuntimeError(
            "Shopify connection foundation tables are missing (" + ", ".join(missing) + "); revision 0121 must "
            "be applied before 0122."
        )
    columns = {c["name"] for c in inspector.get_columns(CONNECTIONS)}
    if not {"id", "tenant_id", "shop_gid", "generation", "status"} <= columns:
        raise RuntimeError("shopify_connections does not have the revision 0121 shape; refusing 0122.")
    present = sorted(names & set(TABLES))
    if present or _connection_key_present(inspector):
        # New in this revision: a pre-existing table or key was not built here
        # and its constraints (tenant-carrying keys, lifecycle and commit
        # evidence checks) cannot be trusted. Refuse without changing anything.
        raise RuntimeError(
            "Shopify catalog objects already exist (" + ", ".join(present or [CONNECTION_KEY]) + "); they were not "
            "created by revision 0122. Inspect and remove them manually before applying this revision."
        )
    op.create_unique_constraint(CONNECTION_KEY, CONNECTIONS, ["id", "tenant_id"])
    definitions = _definitions()
    for name in TABLES:
        definitions[name].create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for name in reversed(TABLES):
        if name in present:
            op.drop_table(name)
    if CONNECTIONS in present and _connection_key_present(inspector):
        op.drop_constraint(CONNECTION_KEY, CONNECTIONS, type_="unique")
