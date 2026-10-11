"""
Shopify catalog mirror tables on their own ``ShopifyCatalogBase`` metadata.

Deliberately outside ``models.Base`` (startup ``create_all``) and outside the
connection foundation's ``ShopifyBase``: nothing here reaches a database until
revision ``0122`` is applied on purpose, and the foundation's own
metadata/migration equivalence (0121) is unchanged. The revision and this
module declare the same schema; ``backend/tests/test_shopify_catalog_import_pg.py``
proves their equivalence on PostgreSQL.

This is a **provider-scoped mirror**, not the shared catalog hub. No row here
is read by the AI runtime, WhatsApp, Meta, campaigns or any channel; nothing
is written to ``products`` / ``product_variants``. Projection into the hub is a
later, separately approved slice.

Identity
────────
Every mirrored resource is identified by ``(connection_id, Shopify GID)``.
``connection_id`` is the foundation's ownership row (one per shop, never
transferred between tenants), so the identity is provider + shop + resource.
Composite foreign keys carry ``tenant_id`` through every parent link:

  run / product / location / inventory item → (connection_id, tenant_id) of
      ``shopify_connections`` (revision 0122 adds that unique key);
  variant → (product_id, connection_id, tenant_id) of its product and
      (inventory_item_id, connection_id, tenant_id) of its inventory item;
  inventory level → its inventory item and its location, the same way;
  ``first_seen_run_id`` / ``last_seen_run_id`` → (run id, connection_id).

So a row can never point at another tenant's or another shop's parent.

Inventory is **item-owned**: since API 2026-10 several variants may share one
inventory item. Levels are keyed by (inventory item, location) — never by
variant — so shared stock is stored once and a level is never rebound to a
different variant.

Lifecycle
─────────
Rows are never hard-deleted by the importer (local references survive).
Products, variants and locations become ``deleted`` only after a complete
traversal **and** a direct lookup confirming Shopify no longer returns them.
Inventory items and levels become ``detached`` when a complete snapshot no
longer references them. Absence from a partial, failed or cancelled run
changes nothing: such runs publish nothing at all.

PostgreSQL semantics are assumed (JSONB, NUMERIC, partial indexes, row locks).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

ShopifyCatalogBase = declarative_base()

# Reference-only stubs so foreign keys resolve inside this metadata. Never
# created from here: the application owns ``tenants``; revision 0121 owns
# ``shopify_connections`` and revision 0122 adds its (id, tenant_id) key.
tenants_reference = Table("tenants", ShopifyCatalogBase.metadata, Column("id", Integer, primary_key=True))
connections_reference = Table(
    "shopify_connections", ShopifyCatalogBase.metadata,
    Column("id", BigInteger, primary_key=True),
    Column("tenant_id", Integer, nullable=False),
    UniqueConstraint("id", "tenant_id", name="uq_shopify_connections_id_tenant"),
)

CONNECTION_TENANT_KEY = "uq_shopify_connections_id_tenant"

RUNS_TABLE = "shopify_catalog_import_runs"
STAGED_TABLE = "shopify_catalog_staged_records"
PRODUCTS_TABLE = "shopify_catalog_products"
ITEMS_TABLE = "shopify_catalog_inventory_items"
VARIANTS_TABLE = "shopify_catalog_variants"
LOCATIONS_TABLE = "shopify_catalog_locations"
LEVELS_TABLE = "shopify_catalog_inventory_levels"

RUN_RUNNING = "running"
RUN_COMMITTED = "committed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_STATUSES = (RUN_RUNNING, RUN_COMMITTED, RUN_FAILED, RUN_CANCELLED)

INVENTORY_PER_LOCATION = "per_location"
INVENTORY_SCOPE_MISSING = "scope_missing"
INVENTORY_MODES = (INVENTORY_PER_LOCATION, INVENTORY_SCOPE_MISSING)

KIND_PRODUCT = "product"
KIND_VARIANT = "variant"
KIND_ITEM = "inventory_item"
KIND_LOCATION = "location"
KIND_LEVEL = "inventory_level"
STAGED_KINDS = (KIND_PRODUCT, KIND_VARIANT, KIND_ITEM, KIND_LOCATION, KIND_LEVEL)

PRESENT = "present"
DELETED = "deleted"
DETACHED = "detached"

PRODUCT_STATUSES = ("ACTIVE", "ARCHIVED", "DRAFT", "UNLISTED")
INVENTORY_POLICIES = ("DENY", "CONTINUE")
TRACKING_STATES = ("tracked", "untracked")
COMBINED_LISTING_ROLES = ("PARENT", "CHILD")

PRODUCT_GID_RE = r"^gid://shopify/Product/[1-9][0-9]{0,19}$"
VARIANT_GID_RE = r"^gid://shopify/ProductVariant/[1-9][0-9]{0,19}$"
ITEM_GID_RE = r"^gid://shopify/InventoryItem/[1-9][0-9]{0,19}$"
LOCATION_GID_RE = r"^gid://shopify/Location/[1-9][0-9]{0,19}$"
LEVEL_GID_RE = r"^gid://shopify/InventoryLevel/[1-9][0-9]{0,19}\?inventory_item_id=[1-9][0-9]{0,19}$"
SHOP_GID_RE = r"^gid://shopify/Shop/[1-9][0-9]{0,19}$"
API_VERSION_RE = r"^20[2-9][0-9]-(01|04|07|10)$"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _ts(name: str, nullable: bool = True, default: bool = False) -> Column:
    kwargs = {"default": _now, "server_default": text("now()")} if default else {}
    return Column(name, DateTime(timezone=True), nullable=nullable, **kwargs)


def _connection_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["connection_id", "tenant_id"], ["shopify_connections.id", "shopify_connections.tenant_id"],
        name=f"fk_{table}_connection",
    )


def _run_fks(table: str):
    return (
        ForeignKeyConstraint(["first_seen_run_id", "connection_id"],
                             [f"{RUNS_TABLE}.id", f"{RUNS_TABLE}.connection_id"],
                             name=f"fk_{table}_first_run"),
        ForeignKeyConstraint(["last_seen_run_id", "connection_id"],
                             [f"{RUNS_TABLE}.id", f"{RUNS_TABLE}.connection_id"],
                             name=f"fk_{table}_last_run"),
    )


def _lifecycle_check(table: str, removed: str) -> CheckConstraint:
    return CheckConstraint(
        f"(lifecycle = 'present' AND removed_at IS NULL) OR (lifecycle = '{removed}' AND removed_at IS NOT NULL)",
        name=f"ck_{table}_lifecycle",
    )


def _mirror_columns():
    return (
        Column("lifecycle", String(16), nullable=False),
        _ts("removed_at"),
        Column("first_seen_run_id", BigInteger, nullable=False),
        Column("last_seen_run_id", BigInteger, nullable=False),
        _ts("created_at", nullable=False, default=True),
        _ts("updated_at", nullable=False, default=True),
    )


class ShopifyCatalogImportRun(ShopifyCatalogBase):
    """One import attempt for one connection. At most one ``running`` per
    connection (partial unique index); only a ``committed`` run ever changed
    the mirror, and it did so in one transaction."""

    __tablename__ = RUNS_TABLE
    __table_args__ = (
        _connection_fk(RUNS_TABLE),
        UniqueConstraint("connection_id", "run_seq", name="uq_shopify_catalog_runs_seq"),
        UniqueConstraint("id", "connection_id", name="uq_shopify_catalog_runs_id_connection"),
        CheckConstraint(f"status IN ({_in(RUN_STATUSES)})", name="ck_shopify_catalog_runs_status"),
        CheckConstraint(f"inventory_mode IS NULL OR inventory_mode IN ({_in(INVENTORY_MODES)})",
                        name="ck_shopify_catalog_runs_inventory_mode"),
        CheckConstraint(f"api_version ~ '{API_VERSION_RE}'", name="ck_shopify_catalog_runs_api_version"),
        CheckConstraint(f"shop_gid ~ '{SHOP_GID_RE}'", name="ck_shopify_catalog_runs_shop_gid"),
        CheckConstraint("run_seq >= 1 AND generation >= 1", name="ck_shopify_catalog_runs_versions"),
        CheckConstraint(
            "(status = 'running' AND lease_id IS NOT NULL AND lease_expires_at IS NOT NULL"
            " AND finished_at IS NULL AND failure_reason IS NULL AND committed_at IS NULL)"
            " OR (status <> 'running' AND lease_id IS NULL AND lease_expires_at IS NULL AND finished_at IS NOT NULL)",
            name="ck_shopify_catalog_runs_lease",
        ),
        CheckConstraint(
            "(status IN ('failed', 'cancelled') AND failure_reason IS NOT NULL AND committed_at IS NULL)"
            " OR status IN ('running', 'committed')",
            name="ck_shopify_catalog_runs_failure",
        ),
        # A committed run is complete evidence: version, currency, mode,
        # traversal flag and counts; inventory counts exist exactly when the
        # inventory was actually read per location.
        CheckConstraint(
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
        CheckConstraint(
            "coalesce(products_count, 0) >= 0 AND coalesce(variants_count, 0) >= 0"
            " AND coalesce(inventory_items_count, 0) >= 0 AND coalesce(locations_count, 0) >= 0"
            " AND coalesce(inventory_levels_count, 0) >= 0 AND pages_fetched >= 0"
            " AND products_removed >= 0 AND variants_removed >= 0 AND stale_skipped >= 0",
            name="ck_shopify_catalog_runs_counts",
        ),
        Index("uq_shopify_catalog_runs_one_running", "connection_id", unique=True,
              postgresql_where=text("status = 'running'")),
        Index("ix_shopify_catalog_runs_tenant_connection", "tenant_id", "connection_id", "run_seq"),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    run_seq = Column(BigInteger, nullable=False)
    shop_domain = Column(String(255), nullable=False)
    shop_gid = Column(String(64), nullable=False)
    generation = Column(BigInteger, nullable=False)
    api_version = Column(String(7), nullable=False)
    status = Column(String(16), nullable=False)
    failure_reason = Column(String(48), nullable=True)
    inventory_mode = Column(String(24), nullable=True)
    lease_id = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    shop_currency = Column(String(3), nullable=True)
    expected_products = Column(Integer, nullable=True)
    traversal_complete = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    pages_fetched = Column(Integer, nullable=False, default=0, server_default=text("0"))
    products_count = Column(Integer, nullable=True)
    variants_count = Column(Integer, nullable=True)
    inventory_items_count = Column(Integer, nullable=True)
    locations_count = Column(Integer, nullable=True)
    inventory_levels_count = Column(Integer, nullable=True)
    products_removed = Column(Integer, nullable=False, default=0, server_default=text("0"))
    variants_removed = Column(Integer, nullable=False, default=0, server_default=text("0"))
    stale_skipped = Column(Integer, nullable=False, default=0, server_default=text("0"))
    started_at = _ts("started_at", nullable=False, default=True)
    heartbeat_at = _ts("heartbeat_at")
    finished_at = _ts("finished_at")
    committed_at = _ts("committed_at")

    def __repr__(self) -> str:
        return (f"ShopifyCatalogImportRun(id={self.id!r}, connection_id={self.connection_id!r}, "
                f"run_seq={self.run_seq!r}, status={self.status!r})")


class ShopifyCatalogStagedRecord(ShopifyCatalogBase):
    """Validated, normalised records of one running import, staged page by
    page. Read only by that run's own publish; deleted when it finishes.
    Never a published value."""

    __tablename__ = STAGED_TABLE
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "gid", name="uq_shopify_catalog_staged_identity"),
        CheckConstraint(f"kind IN ({_in(STAGED_KINDS)})", name="ck_shopify_catalog_staged_kind"),
    )

    id = Column(BigInteger, primary_key=True)
    run_id = Column(BigInteger, ForeignKey(f"{RUNS_TABLE}.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)
    gid = Column(String(128), nullable=False)
    parent_gid = Column(String(128), nullable=True)
    payload = Column(JSONB, nullable=False)


class ShopifyCatalogProduct(ShopifyCatalogBase):
    __tablename__ = PRODUCTS_TABLE
    __table_args__ = (
        _connection_fk(PRODUCTS_TABLE),
        *_run_fks(PRODUCTS_TABLE),
        UniqueConstraint("connection_id", "product_gid", name="uq_shopify_catalog_products_identity"),
        UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_products_parent_key"),
        CheckConstraint(f"product_gid ~ '{PRODUCT_GID_RE}'", name="ck_shopify_catalog_products_gid"),
        CheckConstraint(f"status IN ({_in(PRODUCT_STATUSES)})", name="ck_shopify_catalog_products_status"),
        CheckConstraint(f"combined_listing_role IS NULL OR combined_listing_role IN ({_in(COMBINED_LISTING_ROLES)})",
                        name="ck_shopify_catalog_products_combined_role"),
        CheckConstraint("variants_count >= 0", name="ck_shopify_catalog_products_variants_count"),
        _lifecycle_check(PRODUCTS_TABLE, DELETED),
        Index("ix_shopify_catalog_products_tenant", "tenant_id", "connection_id", "lifecycle"),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    product_gid = Column(String(64), nullable=False)
    title = Column(String(255), nullable=False)
    handle = Column(String(255), nullable=False)
    status = Column(String(16), nullable=False)
    vendor = Column(String(255), nullable=True)
    product_type = Column(String(255), nullable=True)
    options = Column(JSONB, nullable=False)
    image_url = Column(String(2048), nullable=True)
    image_alt = Column(String(512), nullable=True)
    images = Column(JSONB, nullable=False)
    online_store_published_at = Column(DateTime(timezone=True), nullable=True)
    is_gift_card = Column(Boolean, nullable=False)
    requires_selling_plan = Column(Boolean, nullable=False)
    combined_listing_role = Column(String(16), nullable=True)
    has_only_default_variant = Column(Boolean, nullable=False)
    variants_count = Column(Integer, nullable=False)
    source_created_at = Column(DateTime(timezone=True), nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle, removed_at, first_seen_run_id, last_seen_run_id, created_at, updated_at = _mirror_columns()


class ShopifyCatalogInventoryItem(ShopifyCatalogBase):
    __tablename__ = ITEMS_TABLE
    __table_args__ = (
        _connection_fk(ITEMS_TABLE),
        *_run_fks(ITEMS_TABLE),
        UniqueConstraint("connection_id", "inventory_item_gid", name="uq_shopify_catalog_items_identity"),
        UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_items_parent_key"),
        CheckConstraint(f"inventory_item_gid ~ '{ITEM_GID_RE}'", name="ck_shopify_catalog_items_gid"),
        CheckConstraint(f"tracking IN ({_in(TRACKING_STATES)})", name="ck_shopify_catalog_items_tracking"),
        _lifecycle_check(ITEMS_TABLE, DETACHED),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    inventory_item_gid = Column(String(64), nullable=False)
    tracking = Column(String(10), nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle, removed_at, first_seen_run_id, last_seen_run_id, created_at, updated_at = _mirror_columns()


class ShopifyCatalogVariant(ShopifyCatalogBase):
    __tablename__ = VARIANTS_TABLE
    __table_args__ = (
        ForeignKeyConstraint(["product_id", "connection_id", "tenant_id"],
                             [f"{PRODUCTS_TABLE}.id", f"{PRODUCTS_TABLE}.connection_id", f"{PRODUCTS_TABLE}.tenant_id"],
                             name="fk_shopify_catalog_variants_product"),
        ForeignKeyConstraint(["inventory_item_id", "connection_id", "tenant_id"],
                             [f"{ITEMS_TABLE}.id", f"{ITEMS_TABLE}.connection_id", f"{ITEMS_TABLE}.tenant_id"],
                             name="fk_shopify_catalog_variants_item"),
        *_run_fks(VARIANTS_TABLE),
        UniqueConstraint("connection_id", "variant_gid", name="uq_shopify_catalog_variants_identity"),
        CheckConstraint(f"variant_gid ~ '{VARIANT_GID_RE}'", name="ck_shopify_catalog_variants_gid"),
        CheckConstraint(f"inventory_policy IN ({_in(INVENTORY_POLICIES)})",
                        name="ck_shopify_catalog_variants_inventory_policy"),
        CheckConstraint("currency_code ~ '^[A-Z]{3}$'", name="ck_shopify_catalog_variants_currency"),
        CheckConstraint("price >= 0 AND (compare_at_price IS NULL OR compare_at_price >= 0)",
                        name="ck_shopify_catalog_variants_price"),
        CheckConstraint(
            "(image_url IS NULL AND image_source IS NULL AND image_alt IS NULL)"
            " OR (image_url IS NOT NULL AND image_source IN ('variant', 'product'))",
            name="ck_shopify_catalog_variants_image",
        ),
        _lifecycle_check(VARIANTS_TABLE, DELETED),
        Index("ix_shopify_catalog_variants_product", "product_id"),
        Index("ix_shopify_catalog_variants_item", "inventory_item_id"),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    product_id = Column(BigInteger, nullable=False)
    inventory_item_id = Column(BigInteger, nullable=False)
    variant_gid = Column(String(64), nullable=False)
    title = Column(String(255), nullable=False)
    sku = Column(String(255), nullable=True)
    barcode = Column(String(255), nullable=True)
    position = Column(Integer, nullable=False)
    # Exact decimals in the shop's default currency (no float anywhere).
    price = Column(Numeric(), nullable=False)
    compare_at_price = Column(Numeric(), nullable=True)
    currency_code = Column(String(3), nullable=False)
    selected_options = Column(JSONB, nullable=False)
    image_url = Column(String(2048), nullable=True)
    image_alt = Column(String(512), nullable=True)
    image_source = Column(String(8), nullable=True)
    inventory_policy = Column(String(8), nullable=False)
    available_for_sale = Column(Boolean, nullable=False)
    requires_components = Column(Boolean, nullable=False)
    source_created_at = Column(DateTime(timezone=True), nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle, removed_at, first_seen_run_id, last_seen_run_id, created_at, updated_at = _mirror_columns()


class ShopifyCatalogLocation(ShopifyCatalogBase):
    __tablename__ = LOCATIONS_TABLE
    __table_args__ = (
        _connection_fk(LOCATIONS_TABLE),
        *_run_fks(LOCATIONS_TABLE),
        UniqueConstraint("connection_id", "location_gid", name="uq_shopify_catalog_locations_identity"),
        UniqueConstraint("id", "connection_id", "tenant_id", name="uq_shopify_catalog_locations_parent_key"),
        CheckConstraint(f"location_gid ~ '{LOCATION_GID_RE}'", name="ck_shopify_catalog_locations_gid"),
        _lifecycle_check(LOCATIONS_TABLE, DELETED),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    location_gid = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False)
    fulfills_online_orders = Column(Boolean, nullable=False)
    lifecycle, removed_at, first_seen_run_id, last_seen_run_id, created_at, updated_at = _mirror_columns()


class ShopifyCatalogInventoryLevel(ShopifyCatalogBase):
    __tablename__ = LEVELS_TABLE
    __table_args__ = (
        ForeignKeyConstraint(["inventory_item_id", "connection_id", "tenant_id"],
                             [f"{ITEMS_TABLE}.id", f"{ITEMS_TABLE}.connection_id", f"{ITEMS_TABLE}.tenant_id"],
                             name="fk_shopify_catalog_levels_item"),
        ForeignKeyConstraint(["location_id", "connection_id", "tenant_id"],
                             [f"{LOCATIONS_TABLE}.id", f"{LOCATIONS_TABLE}.connection_id",
                              f"{LOCATIONS_TABLE}.tenant_id"],
                             name="fk_shopify_catalog_levels_location"),
        *_run_fks(LEVELS_TABLE),
        UniqueConstraint("inventory_item_id", "location_id", name="uq_shopify_catalog_levels_item_location"),
        UniqueConstraint("connection_id", "inventory_level_gid", name="uq_shopify_catalog_levels_identity"),
        CheckConstraint(f"inventory_level_gid ~ '{LEVEL_GID_RE}'", name="ck_shopify_catalog_levels_gid"),
        _lifecycle_check(LEVELS_TABLE, DETACHED),
    )

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    connection_id = Column(BigInteger, nullable=False)
    inventory_item_id = Column(BigInteger, nullable=False)
    location_id = Column(BigInteger, nullable=False)
    inventory_level_gid = Column(String(128), nullable=False)
    # Shopify's ``available`` quantity at this one location (may be negative
    # when oversold under a CONTINUE policy). Never summed here.
    available = Column(Integer, nullable=False)
    source_updated_at = Column(DateTime(timezone=True), nullable=False)
    lifecycle, removed_at, first_seen_run_id, last_seen_run_id, created_at, updated_at = _mirror_columns()


CATALOG_TABLES = (
    ShopifyCatalogImportRun.__table__,
    ShopifyCatalogStagedRecord.__table__,
    ShopifyCatalogProduct.__table__,
    ShopifyCatalogInventoryItem.__table__,
    ShopifyCatalogVariant.__table__,
    ShopifyCatalogLocation.__table__,
    ShopifyCatalogInventoryLevel.__table__,
)
CATALOG_TABLE_NAMES = tuple(t.name for t in CATALOG_TABLES)
MIRROR_TABLE_NAMES = (PRODUCTS_TABLE, ITEMS_TABLE, VARIANTS_TABLE, LOCATIONS_TABLE, LEVELS_TABLE)


def add_connection_tenant_key(bind) -> None:
    """The (id, tenant_id) key on ``shopify_connections`` the composite FKs need."""
    bind.execute(text(
        f"ALTER TABLE shopify_connections ADD CONSTRAINT {CONNECTION_TENANT_KEY} UNIQUE (id, tenant_id)"
    ))


def create_catalog_tables(bind) -> None:
    """Test/provisioning helper: the catalog tables only (never tenants or
    the foundation tables). The connection key must already exist."""
    ShopifyCatalogBase.metadata.create_all(bind, tables=list(CATALOG_TABLES))
