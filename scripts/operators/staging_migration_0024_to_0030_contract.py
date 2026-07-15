"""Closed contract for staging legacy migration 0024 → 0030 operator gates."""
from __future__ import annotations

BASE_REVISION = "0024"
TARGET_REVISION = "0030"

CONFIRMATION_TOKEN = "RUN_STAGING_0024_TO_0030"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0024_TO_0030_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800

REQUIRED_TABLES = (
    "product_interests",
    "promotions",
    "offer_decisions",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "product_interests": (
        "ix_product_interests_pending",
        "uq_product_interest_pending_per_customer",
    ),
    "orders": (
        "ix_orders_external_order_number",
        "ix_orders_source",
    ),
    "smart_automations": ("ix_smart_automations_engine",),
    "promotions": (
        "ix_promotions_tenant_id",
        "ix_promotions_status",
        "ix_promotions_tenant_status",
        "ix_promotions_tenant_type",
    ),
    "offer_decisions": (
        "ix_offer_decisions_tenant_id",
        "ix_offer_decisions_decision_id",
        "ix_offer_decisions_automation_id",
        "ix_offer_decisions_customer_id",
        "ix_offer_decisions_tenant_created",
        "ix_offer_decisions_tenant_surface",
        "ix_offer_decisions_tenant_chosen",
        "ix_offer_decisions_tenant_attributed",
        "uq_offer_decisions_decision_id",
    ),
    "tenants": ("ix_tenants_is_platform_tenant",),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "products": ("stock_quantity", "in_stock"),
    "orders": ("external_order_number", "customer_name", "source"),
    "smart_automations": ("engine",),
    "tenants": ("is_platform_tenant",),
}

# automation_type values 0027 backfills via ENGINE_BY_TYPE.
ENGINE_BACKFILL_AUTOMATION_TYPES = (
    "abandoned_cart",
    "customer_winback",
    "unpaid_order_reminder",
    "vip_upgrade",
    "predictive_reorder",
    "new_product_alert",
    "back_in_stock",
    "seasonal_offer",
    "salary_payday_offer",
)
