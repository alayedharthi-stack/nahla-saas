"""Closed contract for staging legacy migration 0083 → 0087 operator gates (A1-Expand).

Repository note: migration 0089 (`0089_conversation_a1_subject_bindings.py`) is
merged on origin/main (PR #596) but is explicitly out of scope for this operator
slice. Alembic repository head may be 0089 while staging runners stop at 0087.
Staging advancement 0087→0089 is a separate later operator slice. Migration 0088
(A1-Validate) is deferred/out of scope.
"""
from __future__ import annotations

BASE_REVISION = "0083"
TARGET_REVISION = "0087"

CONFIRMATION_TOKEN = "RUN_STAGING_0083_TO_0087"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0083_TO_0087_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800
MIN_MIGRATION_TIMEOUT_SEC = 300
MAX_MIGRATION_TIMEOUT_SEC = 3600

# 0084 partial unique index hazard (non-empty external_id per tenant).
MAX_DUPLICATE_TENANT_PRODUCT_EXTERNAL_ID_GROUPS = 0
PRODUCTS_TENANT_EXTERNAL_ID_INDEX = "uq_products_tenant_external_id_nonempty"

REPOSITORY_MERGED_BUT_OUT_OF_SCOPE_REVISIONS = ("0088", "0089")

REQUIRED_TABLES = (
    "external_customer_profiles",
    "external_customer_profile_order_history_coverage",
    "nahla_internal_customer_order_history_coverage",
    "order_customer_identity_capability_state",
)

REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "customers": ("uq_customers_tenant_id",),
    "integrations": ("uq_integrations_tenant_id_id",),
    "products": (PRODUCTS_TENANT_EXTERNAL_ID_INDEX,),
    "external_customer_profiles": (
        "uq_external_customer_profiles_identity",
        "uq_external_customer_profiles_tenant_id_connection",
        "ix_ecp_tenant_integration",
    ),
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": (
        "customer_id",
        "order_source_kind",
        "identity_namespace",
        "integration_connection_id",
        "external_customer_ref",
        "external_customer_profile_id",
        "customer_link_state",
        "customer_link_evidence_class",
        "customer_link_source",
        "customer_linked_at",
        "external_identity_link_state",
        "external_identity_evidence_class",
    ),
}

REQUIRED_NOT_VALID_CHECK_CONSTRAINTS = (
    "chk_orders_external_no_canonical_customer",
    "chk_orders_external_profile_authoritative",
    "chk_orders_external_no_customer_link_authoritative",
    "chk_orders_nahla_internal_authoritative",
    "chk_orders_internal_no_external_authoritative",
    "chk_orders_untrusted_no_authoritative",
    "chk_orders_untrusted_kinds_no_links",
)

REQUIRED_NOT_VALID_FOREIGN_KEYS = (
    "fk_orders_tenant_customer",
    "fk_orders_external_profile_connection",
    "fk_ecp_tenant_integration",
)

DEFERRED_ORDER_INDEXES = (
    "ix_orders_tenant_customer_id",
    "ix_orders_tenant_external_tuple",
    "ix_orders_tenant_order_source_kind",
)

CAPABILITY_KEY = "order_customer_identity"
CAPABILITY_STATE_EXPAND = "expand"

REQUIRED_EXTENSIONS: tuple[str, ...] = ()

CATALOG_AUDIT_FORBIDDEN_INDICATORS = (
    "external_customer_profiles",
    "order_customer_identity_capability_state",
    "conversation_a1_subject_bindings",
)
