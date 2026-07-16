"""Closed contract for staging legacy migration 0032 → 0083 operator gates."""
from __future__ import annotations

BASE_REVISION = "0032"
TARGET_REVISION = "0083"

CONFIRMATION_TOKEN = "RUN_STAGING_0032_TO_0083"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0032_TO_0083_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

# 0064 variant backfill can be heavy on large catalogs; bounded operator window.
DEFAULT_MIGRATION_TIMEOUT_SEC = 3600
MIN_MIGRATION_TIMEOUT_SEC = 600
MAX_MIGRATION_TIMEOUT_SEC = 7200

REQUIRED_TABLES = (
    "product_variants",
    "product_groups",
    "product_group_items",
    "product_relations",
    "product_rankings",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "product_variants": (
        "ix_variants_tenant_retailer",
        "uq_variants_product_salla",
    ),
    "products": ("ix_products_default_variant",),
    "product_groups": (
        "ix_product_groups_tenant_active",
        "ix_product_groups_tenant_priority",
        "uq_product_groups_tenant_slug",
    ),
    "product_group_items": (
        "ix_product_group_items_group_priority",
        "uq_product_group_items_group_product",
    ),
    "product_relations": (
        "ix_product_relations_tenant_source",
        "ix_product_relations_tenant_target",
        "uq_product_relations_tenant_pair_type",
    ),
    "product_rankings": (
        "ix_product_rankings_tenant_best_seller",
        "uq_product_rankings_tenant_product",
    ),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("has_variants", "default_variant_id"),
}
