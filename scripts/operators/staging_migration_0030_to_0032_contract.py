"""Closed contract for staging legacy migration 0030 → 0032 operator gates."""
from __future__ import annotations

BASE_REVISION = "0030"
TARGET_REVISION = "0032"

CONFIRMATION_TOKEN = "RUN_STAGING_0030_TO_0032"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0030_TO_0032_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800

# Hard-stop thresholds for 0031/0032 duplicate preflight (aggregate-only).
MAX_DUPLICATE_TENANT_PHONE_GROUPS = 0
MAX_DUPLICATE_TENANT_SALLA_METADATA_GROUPS = 0
# 0031 backfill: COALESCE(metadata->>'salla_id', metadata->>'external_id')
MAX_DUPLICATE_TENANT_SALLA_BACKFILL_GROUPS = 0
MAX_DUPLICATE_TENANT_NORMALIZED_PHONE_GROUPS = 0

REQUIRED_TABLES: tuple[str, ...] = ()

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "customers": (
        "ix_customers_tenant_id",
        "ix_customers_tenant_normalized_phone",
        "ix_customers_tenant_salla_id",
        "ix_customers_acquisition_channel",
    ),
    "integrations": ("ix_integrations_provider_store_notnull",),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "customers": (
        "salla_customer_id",
        "acquisition_channel",
        "first_seen_at",
        "last_interaction_at",
        "normalized_phone",
    ),
}
