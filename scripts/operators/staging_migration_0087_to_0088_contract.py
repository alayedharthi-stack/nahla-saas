"""Closed contract for staging A1-Validate migration 0087 → 0088 operator gates.

Repository note: migration ``0089`` (`0089_conversation_a1_subject_bindings.py`) remains
a sibling branch from ``0087``. This operator never targets ``0089`` or ``head``.
Staging at ``0087`` advances to ``0088`` only after G4 ``ready_for_validate`` evidence.
"""
from __future__ import annotations

BASE_REVISION = "0087"
TARGET_REVISION = "0088"

CONFIRMATION_TOKEN = "RUN_STAGING_0087_TO_0088"
CONFIRMATION_ENV = "NAHLA_STAGING_MIGRATION_0087_TO_0088_CONFIRM"
BOOTSTRAP_FREEZE_ENV = "NAHLA_SKIP_DB_BOOTSTRAP"

STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

DEFAULT_MIGRATION_TIMEOUT_SEC = 1800
MIN_MIGRATION_TIMEOUT_SEC = 300
MAX_MIGRATION_TIMEOUT_SEC = 3600

REPOSITORY_SIBLING_OUT_OF_SCOPE_REVISIONS = ("0089",)

FORBIDDEN_POST_0087_TABLES = ("conversation_a1_subject_bindings",)

CAPABILITY_KEY = "order_customer_identity"
CAPABILITY_STATE_EXPAND = "expand"
CAPABILITY_STATE_VALIDATED = "validated"
VALIDATION_REVISION = "0088"

ORDER_CHECK_CONSTRAINTS = (
    "chk_orders_external_no_canonical_customer",
    "chk_orders_external_profile_authoritative",
    "chk_orders_external_no_customer_link_authoritative",
    "chk_orders_nahla_internal_authoritative",
    "chk_orders_internal_no_external_authoritative",
    "chk_orders_untrusted_no_authoritative",
    "chk_orders_untrusted_kinds_no_links",
)

ORDER_FOREIGN_KEYS = (
    "fk_orders_tenant_customer",
    "fk_orders_external_profile_connection",
)

ORDER_CONSTRAINTS = ORDER_CHECK_CONSTRAINTS + ORDER_FOREIGN_KEYS

DEFERRED_ORDER_INDEXES = (
    "ix_orders_tenant_customer_id",
    "ix_orders_tenant_external_tuple",
    "ix_orders_tenant_order_source_kind",
)

# Privacy-safe aggregate violation probes (counts only; no row identifiers).
CONSTRAINT_VIOLATION_PROBES: dict[str, str] = {
    "chk_orders_external_no_canonical_customer": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            order_source_kind IS DISTINCT FROM 'external_provider'
            OR (
                customer_id IS NULL
                AND (customer_link_state = 'unlinked' OR customer_link_state IS NULL)
                AND customer_link_evidence_class IS NULL
            )
        )
    """,
    "chk_orders_external_profile_authoritative": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            NOT (
                order_source_kind = 'external_provider'
                AND external_identity_evidence_class = 'authoritative'
            )
            OR (
                external_identity_link_state = 'verified'
                AND external_customer_profile_id IS NOT NULL
                AND integration_connection_id IS NOT NULL
                AND external_customer_ref IS NOT NULL
                AND identity_namespace LIKE 'external_provider_%'
                AND customer_id IS NULL
                AND customer_link_state = 'unlinked'
                AND customer_link_evidence_class IS NULL
            )
        )
    """,
    "chk_orders_external_no_customer_link_authoritative": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            order_source_kind IS DISTINCT FROM 'external_provider'
            OR customer_link_evidence_class IS DISTINCT FROM 'authoritative'
        )
    """,
    "chk_orders_nahla_internal_authoritative": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            NOT (
                order_source_kind = 'nahla_internal'
                AND customer_link_evidence_class = 'authoritative'
            )
            OR (
                customer_link_state = 'verified'
                AND customer_id IS NOT NULL
                AND identity_namespace = 'nahla_internal_order_v1'
                AND external_identity_link_state IS NULL
                AND external_identity_evidence_class IS NULL
                AND external_customer_profile_id IS NULL
                AND integration_connection_id IS NULL
                AND external_customer_ref IS NULL
            )
        )
    """,
    "chk_orders_internal_no_external_authoritative": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            order_source_kind IS DISTINCT FROM 'nahla_internal'
            OR external_identity_evidence_class IS DISTINCT FROM 'authoritative'
        )
    """,
    "chk_orders_untrusted_no_authoritative": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            order_source_kind IN ('nahla_internal', 'external_provider')
            OR (
                customer_link_evidence_class IS DISTINCT FROM 'authoritative'
                AND external_identity_evidence_class IS DISTINCT FROM 'authoritative'
            )
        )
    """,
    "chk_orders_untrusted_kinds_no_links": """
        SELECT count(*)::int FROM orders
        WHERE NOT (
            order_source_kind NOT IN ('whatsapp', 'manual', 'other')
            OR (
                customer_id IS NULL
                AND external_customer_profile_id IS NULL
                AND customer_link_evidence_class IS NULL
                AND external_identity_evidence_class IS NULL
                AND (customer_link_state = 'unlinked' OR customer_link_state IS NULL)
                AND (external_identity_link_state = 'unlinked' OR external_identity_link_state IS NULL)
                AND integration_connection_id IS NULL
                AND external_customer_ref IS NULL
                AND identity_namespace IS NULL
            )
        )
    """,
    "fk_orders_tenant_customer": """
        SELECT count(*)::int FROM orders o
        WHERE o.customer_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM customers c
              WHERE c.tenant_id = o.tenant_id AND c.id = o.customer_id
          )
    """,
    "fk_orders_external_profile_connection": """
        SELECT count(*)::int FROM orders o
        WHERE o.external_customer_profile_id IS NOT NULL
          AND o.integration_connection_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM external_customer_profiles ecp
              WHERE ecp.tenant_id = o.tenant_id
                AND ecp.id = o.external_customer_profile_id
                AND ecp.integration_connection_id = o.integration_connection_id
          )
    """,
}

MANIFEST_SCHEMA_VERSION = "staging_migration_0087_to_0088_v1"
