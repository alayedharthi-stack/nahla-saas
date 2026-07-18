"""Closed contract for selective merchant-plane tenant clone (Tenant 33 acceptance).

Default-off operator: dry-run only unless all fail-closed gates pass. Source
connection is read-only; target rolls back on any failure. Production source
requires an additional exact confirmation token — execution against production
remains blocked pending separate owner approval.

Full DR restore is forbidden for acceptance cloning. See
``docs/engineering/tenant-merchant-clone-runbook.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple

MANIFEST_SCHEMA_VERSION = "tenant_merchant_clone_v3"
DRY_RUN_DIGEST_SCHEMA_VERSION = "tenant_merchant_clone_dry_run_v10"

# Closed clone profiles — ``--profile`` is required; no default (fail closed).
CLONE_PROFILE_FULL_MERCHANT = "full_merchant_acceptance"
CLONE_PROFILE_SALLA_MINIMAL = "salla_acceptance_minimal"
KNOWN_CLONE_PROFILES: FrozenSet[str] = frozenset(
    {CLONE_PROFILE_FULL_MERCHANT, CLONE_PROFILE_SALLA_MINIMAL}
)
DEFAULT_ACCEPTANCE_TENANT_ID = 33
PRESERVE_TENANT_IDENTITY_MODE = "preserve_tenant_id_cross_database"
REMAP_TENANT_IDENTITY_MODE = "remap_tenant_id_cross_database"
TARGET_BOOTSTRAP_NAME = "tenant-33-acceptance-test"

# ── Environment identity attestation ─────────────────────────────────────────
SOURCE_PROJECT_ENV = "NAHLA_CLONE_SOURCE_RAILWAY_PROJECT"
SOURCE_ENVIRONMENT_ENV = "NAHLA_CLONE_SOURCE_RAILWAY_ENVIRONMENT"
TARGET_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
TARGET_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"

STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
PRODUCTION_ENVIRONMENT_VALUE = "production"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"
PRODUCTION_IDENTITY_CLASS = "railway_production_desirable_growth"

FORBIDDEN_ENV_MARKERS = frozenset({"production", "prod", "live"})
_ALLOWED_STAGING_DATABASE_HOST = "postgres-staging.railway.internal"
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgresql+psycopg2"})

# Target must be experimental staging — never production.
TARGET_ALLOWED_ENVIRONMENT_VALUES = frozenset({STAGING_ENVIRONMENT_VALUE})
TARGET_TEST_SLUG_MARKERS = frozenset({"-acceptance-test", "-clone-test", "-tenant33-test"})

# ── Master execution gates (default off) ───────────────────────────────────
MASTER_ENABLE_ENV = "NAHLA_TENANT_MERCHANT_CLONE_ENABLED"
APPLY_CONFIRM_ENV = "NAHLA_TENANT_MERCHANT_CLONE_APPLY_CONFIRM"
APPLY_CONFIRM_TOKEN = "APPLY_TENANT_33_MERCHANT_CLONE"
CLEANUP_CONFIRM_ENV = "NAHLA_TENANT_MERCHANT_CLONE_CLEANUP_CONFIRM"
CLEANUP_CONFIRM_TOKEN = "CLEANUP_TENANT_33_MERCHANT_CLONE"
PRODUCTION_SOURCE_CONFIRM_ENV = "NAHLA_TENANT_CLONE_PRODUCTION_SOURCE_CONFIRM"
PRODUCTION_SOURCE_CONFIRM_TOKEN = (
    "CLONE_PRODUCTION_TENANT_33_TO_STAGING_TENANT_33"
)
DRY_RUN_DIGEST_ENV = "NAHLA_TENANT_MERCHANT_CLONE_DRY_RUN_DIGEST"

SOURCE_DATABASE_URL_ENV = "NAHLA_CLONE_SOURCE_DATABASE_URL"
TARGET_DATABASE_URL_ENV = "DATABASE_URL"

KNOWN_ALEMBIC_REVISIONS = frozenset({"0088", "0089"})
EXPECTED_SOURCE_ALEMBIC_HEADS = frozenset({"0089"})
EXPECTED_TARGET_ALEMBIC_HEADS = frozenset({"0088", "0089"})

# ── Tenant scalar columns (public merchant settings only) ────────────────────
TENANT_COPY_COLUMNS: Tuple[str, ...] = (
    "store_address",
    "google_maps_link",
    "apple_maps_link",
    "same_day_delivery_enabled",
    "pickup_enabled",
    "branding",
    "recommendation_controls",
    "coupon_policy",
)

TENANT_DENIED_COLUMNS: FrozenSet[str] = frozenset(
    {
        "id",
        "name",
        "domain",
        "is_active",
        "is_platform_tenant",
        "created_at",
        "ai_blocked_numbers",
        "billing_provider",
        "stripe_customer_id",
        "stripe_subscription_id",
        "stripe_price_id",
        "subscription_status",
        "trial_started_at",
        "trial_ends_at",
        "first_whatsapp_connected_at",
        "current_period_end",
        "hyperpay_payment_id",
        "billing_status",
    }
)

# Force safe AI posture on target after clone.
TARGET_AI_MODE = "test"
TARGET_AI_TEST_ALLOWLIST: Tuple[str, ...] = ()

# Columns always nulled/stripped on copy (global/external IDs, credentials).
GLOBAL_STRIP_COLUMNS: FrozenSet[str] = frozenset(
    {
        "external_id",
        "external_store_id",
        "salla_variant_id",
        "meta_retailer_id",
        "meta_item_id",
        "meta_template_id",
        "source_external_id",
        "canonical_retailer_id",
        "retailer_id",
        "meta_catalog_published_at",
        "meta_last_seen_at",
        "meta_removed_at",
        "managed_confirmed_by",
    }
)

RESET_COUNT_COLUMNS: FrozenSet[str] = frozenset(
    {
        "usage_count",
        "stats_triggered",
        "stats_sent",
        "stats_converted",
        "order_count",
        "customer_count",
    }
)


@dataclass(frozen=True)
class CloneTableSpec:
    """One merchant-plane table in closed dependency order."""

    name: str
    tenant_column: str = "tenant_id"
    remap_fk_columns: Tuple[str, ...] = ()
    json_columns: Tuple[str, ...] = ()
    skip_columns: FrozenSet[str] = frozenset()
    upsert_on_tenant: bool = False
    deferred_fk_columns: Tuple[str, ...] = ()
    scrub_phone_columns: Tuple[str, ...] = ()


# Closed allow-list — exact dependency order for full Tenant 33 acceptance scope.
FULL_MERCHANT_TABLE_SPECS: Tuple[CloneTableSpec, ...] = (
    CloneTableSpec("tenant_settings", upsert_on_tenant=True, json_columns=("whatsapp_settings", "ai_settings", "store_settings", "notification_settings", "metadata")),
    CloneTableSpec("commerce_permissions", upsert_on_tenant=True),
    CloneTableSpec("delivery_zones"),
    CloneTableSpec("shipping_fees"),
    CloneTableSpec(
        "products",
        json_columns=("metadata", "recommendation_tags", "source_conflict_detail"),
        deferred_fk_columns=("default_variant_id",),
    ),
    CloneTableSpec(
        "product_variants",
        remap_fk_columns=("product_id",),
        json_columns=("metadata", "options"),
    ),
    CloneTableSpec("product_groups", json_columns=("metadata_json",)),
    CloneTableSpec(
        "product_group_items",
        remap_fk_columns=("group_id", "product_id", "variant_id"),
    ),
    CloneTableSpec(
        "product_relations",
        remap_fk_columns=("source_product_id", "target_product_id"),
    ),
    CloneTableSpec("product_rankings", remap_fk_columns=("product_id",)),
    CloneTableSpec("ai_media_library", json_columns=("tags",)),
    CloneTableSpec("merchant_knowledge_sections", json_columns=("metadata_json", "conflicts_json")),
    CloneTableSpec(
        "merchant_knowledge_media",
        remap_fk_columns=("section_id", "media_id"),
    ),
    CloneTableSpec(
        "merchant_knowledge_section_products",
        remap_fk_columns=("section_id", "product_id"),
    ),
    CloneTableSpec("coupons", json_columns=("metadata",)),
    CloneTableSpec("coupon_rules", remap_fk_columns=("coupon_id",), json_columns=("rule_config",)),
    CloneTableSpec("promotions", json_columns=("metadata", "conditions")),
    CloneTableSpec("manual_coupons"),
    CloneTableSpec("whatsapp_templates", json_columns=("components", "ai_generation_metadata")),
    CloneTableSpec(
        "smart_automations",
        remap_fk_columns=("template_id",),
        json_columns=("config",),
    ),
    CloneTableSpec("automation_rules", json_columns=("trigger_config", "action_config")),
    CloneTableSpec("merchant_branches", json_columns=("hours_json",)),
    CloneTableSpec(
        "branch_contacts",
        remap_fk_columns=("branch_id",),
        scrub_phone_columns=("phone_e164", "whatsapp_e164"),
    ),
    CloneTableSpec(
        "branch_escalation_steps",
        remap_fk_columns=("branch_id", "contact_id"),
        scrub_phone_columns=("phone_e164",),
    ),
    CloneTableSpec("branch_arrival_keywords", remap_fk_columns=("branch_id",)),
    CloneTableSpec(
        "knowledge_policies",
        json_columns=("allowed_categories", "blocked_categories", "escalation_rules", "owner_override"),
    ),
    CloneTableSpec("merchant_addons", json_columns=("settings_json",)),
    CloneTableSpec("merchant_widgets", json_columns=("settings_json", "display_rules")),
    CloneTableSpec("widget_settings", json_columns=("options",)),
    CloneTableSpec(
        "integrations",
        json_columns=("config",),
    ),
    CloneTableSpec(
        "store_knowledge_snapshots",
        upsert_on_tenant=True,
        json_columns=(
            "store_profile",
            "catalog_summary",
            "shipping_summary",
            "policy_summary",
            "coupon_summary",
        ),
    ),
)

# Minimal Salla acceptance — catalog, KB, shipping, scrubbed integration metadata only.
SALLA_ACCEPTANCE_MINIMAL_TABLE_SPECS: Tuple[CloneTableSpec, ...] = (
    CloneTableSpec("tenant_settings", upsert_on_tenant=True, json_columns=("whatsapp_settings", "ai_settings", "store_settings", "notification_settings", "metadata")),
    CloneTableSpec("commerce_permissions", upsert_on_tenant=True),
    CloneTableSpec("delivery_zones"),
    CloneTableSpec("shipping_fees"),
    CloneTableSpec(
        "products",
        json_columns=("metadata", "recommendation_tags", "source_conflict_detail"),
        deferred_fk_columns=("default_variant_id",),
    ),
    CloneTableSpec(
        "product_variants",
        remap_fk_columns=("product_id",),
        json_columns=("metadata", "options"),
    ),
    CloneTableSpec("product_groups", json_columns=("metadata_json",)),
    CloneTableSpec(
        "product_group_items",
        remap_fk_columns=("group_id", "product_id", "variant_id"),
    ),
    CloneTableSpec(
        "product_relations",
        remap_fk_columns=("source_product_id", "target_product_id"),
    ),
    CloneTableSpec("ai_media_library", json_columns=("tags",)),
    CloneTableSpec("merchant_knowledge_sections", json_columns=("metadata_json", "conflicts_json")),
    CloneTableSpec(
        "merchant_knowledge_media",
        remap_fk_columns=("section_id", "media_id"),
    ),
    CloneTableSpec(
        "merchant_knowledge_section_products",
        remap_fk_columns=("section_id", "product_id"),
    ),
    CloneTableSpec(
        "integrations",
        json_columns=("config",),
    ),
)

PROFILE_TABLE_SPECS: dict[str, Tuple[CloneTableSpec, ...]] = {
    CLONE_PROFILE_FULL_MERCHANT: FULL_MERCHANT_TABLE_SPECS,
    CLONE_PROFILE_SALLA_MINIMAL: SALLA_ACCEPTANCE_MINIMAL_TABLE_SPECS,
}

# Operational merchant config present in full profile but excluded from minimal.
EXCLUDED_OPERATIONAL_TABLES: FrozenSet[str] = frozenset(
    {
        "product_rankings",
        "coupons",
        "coupon_rules",
        "promotions",
        "manual_coupons",
        "whatsapp_templates",
        "smart_automations",
        "automation_rules",
        "merchant_branches",
        "branch_contacts",
        "branch_escalation_steps",
        "branch_arrival_keywords",
        "knowledge_policies",
        "merchant_addons",
        "merchant_widgets",
        "widget_settings",
        # Derived AI cache (store_profile PII, coupon/customer/order summaries) — not authoritative.
        "store_knowledge_snapshots",
    }
)

# Backward-compatible alias for full merchant profile consumers.
ALLOWED_TABLE_SPECS: Tuple[CloneTableSpec, ...] = FULL_MERCHANT_TABLE_SPECS
ALLOWED_TABLE_NAMES: FrozenSet[str] = frozenset(
    spec.name for spec in FULL_MERCHANT_TABLE_SPECS
)


def resolve_clone_profile(profile: str | None) -> str:
    """Fail closed when profile is missing or unknown."""
    normalized = (profile or "").strip()
    if not normalized:
        raise ValueError("clone_profile_missing")
    if normalized not in KNOWN_CLONE_PROFILES:
        raise ValueError("clone_profile_unknown")
    return normalized


def table_specs_for_profile(profile: str) -> Tuple[CloneTableSpec, ...]:
    return PROFILE_TABLE_SPECS[resolve_clone_profile(profile)]


def allowed_table_names_for_profile(profile: str) -> FrozenSet[str]:
    return frozenset(spec.name for spec in table_specs_for_profile(profile))


def excluded_operational_tables_for_profile(profile: str) -> FrozenSet[str]:
    if resolve_clone_profile(profile) == CLONE_PROFILE_SALLA_MINIMAL:
        return EXCLUDED_OPERATIONAL_TABLES
    return frozenset()

# Hard deny — never read for copy; dry-run proves zero writes to these domains.
DENIED_TABLES: FrozenSet[str] = frozenset(
    {
        "users",
        "user_totp",
        "user_recovery_codes",
        "password_setup_tokens",
        "whatsapp_connections",
        "whatsapp_numbers",
        "whatsapp_usage",
        "wa_conversation_windows",
        "conversation_logs",
        "wa_webhook_raw",
        "message_delivery_events",
        "wa_number_quality_snapshots",
        "customers",
        "customer_addresses",
        "customer_profiles",
        "customer_preferences",
        "customer_import_batches",
        "customer_name_cleanup_drafts",
        "customer_name_audit_logs",
        "customer_segments_manual",
        "customer_suppressions",
        "orders",
        "order_shipments",
        "external_customer_profiles",
        "order_customer_identity_capability_state",
        "external_customer_profile_order_history_coverage",
        "nahla_internal_customer_order_history_coverage",
        "conversation_a1_subject_bindings",
        "conversations",
        "message_events",
        "handoff_sessions",
        "conversation_traces",
        "conversation_history_summaries",
        "payment_sessions",
        "offer_decisions",
        "campaigns",
        "campaign_waves",
        "campaign_send_logs",
        "automation_events",
        "automation_executions",
        "governor_send_logs",
        "notification_logs",
        "product_interests",
        "predictive_reorder_estimates",
        "audit_logs",
        "ai_action_logs",
        "ai_quality_events",
        "ai_usage_events",
        "system_events",
        "sync_logs",
        "webhook_events",
        "webhook_guardian_log",
        "integrity_events",
        "store_sync_jobs",
        "billing_plans",
        "billing_subscriptions",
        "billing_payments",
        "billing_invoices",
        "app_installs",
        "app_payments",
        "developers",
        "apps",
        "cross_merchant_signals",
        "learned_sales_policies",
        "salla_trial_ledger",
        "product_affinities",
        "price_sensitivity_scores",
        "commerce_lifecycle_notification_ledger",
        "merchant_knowledge_drafts",
    }
)

# JSON key patterns that abort unless scrubbed by closed transform.
FORBIDDEN_JSON_KEY_MARKERS: Tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "verify_token",
    "api_key",
    "api_secret",
    "client_secret",
    "password",
    "password_hash",
    "secret",
    "oauth",
    "bearer",
    "private_key",
    "phone",
    "phone_number",
    "phone_e164",
    "whatsapp_e164",
    "email",
    "normalized_phone",
    "customer_id",
    "conversation_id",
    "order_id",
    "tracking_number",
    "receipt",
    "payment_id",
    "stripe",
    "hyperpay",
)

# Closed registry — provider routing/ownership handles scrubbed in integrations.config.
PROVIDER_OWNERSHIP_KEYS: FrozenSet[str] = frozenset(
    {
        "store_id",
        "merchant_id",
        "external_store_id",
        "authorization_id",
        "phone_number_id",
        "waba_id",
        "whatsapp_business_account_id",
        "shop_id",
        "seller_id",
        "vendor_id",
        "meta_business_id",
        "meta_catalog_id",
    }
)

SCRUBBED_JSON_KEY_REPLACEMENTS: dict[str, str] = {
    "access_token": "",
    "refresh_token": "",
    "verify_token": "",
    "api_key": "",
    "api_secret": "",
    "client_secret": "",
    "phone": "",
    "phone_number": "",
    "phone_e164": "+00000000000",
    "whatsapp_e164": "",
    "email": "",
    "owner_whatsapp_number": "",
}

PHONE_SCRUB_PLACEHOLDER = "+00000000000"
