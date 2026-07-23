"""Closed contract for staging real-channel acceptance config consolidation.

Consolidates channel/config variables from a legacy staging app service onto the
canonical staging app service within ``desirable-growth`` / ``staging``. Default-off;
no Railway mutations unless all fail-closed gates pass after ARCH-001 shadow teardown.

Railway resource IDs are pinned from authenticated, read-only ``railway list --json``
inventory. Never copy production variables or secret values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping

REPORT_SCHEMA_VERSION = "staging_acceptance_config_consolidation_v1"
SNAPSHOT_SCHEMA_VERSION = "staging_acceptance_config_snapshot_v1"

# ── Staging identity (fail-closed) ───────────────────────────────────────────
STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_ID_ENV = "RAILWAY_PROJECT_ID"
STAGING_ENVIRONMENT_ID_ENV = "RAILWAY_ENVIRONMENT_ID"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

# ── Closed Railway ID allowlist (staging only) ────────────────────────────────
# Source: authenticated read-only `railway list --json`, verified 2026-07-18.
STAGING_RAILWAY_PROJECT_ID = "f0090862-0a40-4293-bd5d-e94df58762b5"
STAGING_RAILWAY_ENVIRONMENT_ID = "b3d51523-7544-4d5c-b510-631b334cd8a7"
PRODUCTION_RAILWAY_ENVIRONMENT_IDS = frozenset(
    {"ede962ce-3042-4dae-94de-623837e83ed9"}
)

CANONICAL_SERVICE_NAME = "nahla-saas"
CANONICAL_SERVICE_ID = "686b36c5-a926-4e58-912a-5e9d13fbc2e7"
LEGACY_SOURCE_SERVICE_NAME = "nahla-saas-staging"
LEGACY_SOURCE_SERVICE_ID = "d0282eea-05fe-49bf-bd58-e663e8585516"

STAGING_SERVICE_ALLOWLIST: dict[str, str] = {
    CANONICAL_SERVICE_NAME: CANONICAL_SERVICE_ID,
    LEGACY_SOURCE_SERVICE_NAME: LEGACY_SOURCE_SERVICE_ID,
}

FORBIDDEN_SERVICE_NAMES = frozenset(
    {
        "nahla-saas-production",
        "creative-intuition",
        "nahla-postgres-prod",
        "nahla-postgres-prod-staging",
    }
)
FORBIDDEN_ENVIRONMENT_MARKERS = frozenset({"production", "prod", "live"})

# ── Master execution gates (default off) ─────────────────────────────────────
MASTER_ENABLE_ENV = "NAHLA_STAGING_ACCEPTANCE_CONFIG_CONSOLIDATION_ENABLED"
APPLY_CONFIRM_ENV = "NAHLA_STAGING_ACCEPTANCE_CONFIG_CONSOLIDATION_CONFIRM"
APPLY_CONFIRM_TOKEN = "consolidate-staging-acceptance-config"
ARCH001_SHADOW_SIGNOFF_ENV = "NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM"
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (  # noqa: E402
    EXPECTED_MANIFEST_DIGEST_ENV as ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST_ENV,
    ISOLATED_DEPLOYMENT_ID_ENV as ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID_ENV,
    ISOLATED_SERVICE_ID_ENV as ARCH001_PREPROD_ISOLATED_SERVICE_ID_ENV,
    ISOLATED_SERVICE_NAME_ENV as ARCH001_PREPROD_ISOLATED_SERVICE_NAME_ENV,
    PINNED_REVISION_ENV as ARCH001_PREPROD_PINNED_REVISION_ENV,
    SIGNOFF_ARTIFACT_ENV as ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV as ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV,
)
ARCH001_TEARDOWN_PROOF_ENV = "NAHLA_ARCH001_SHADOW_TEARDOWN_PROOF"
SNAPSHOT_KEY_ENV = "NAHLA_STAGING_ACCEPTANCE_CONFIG_SNAPSHOT_KEY"
PINNED_REVISION_ENV = "NAHLA_STAGING_ACCEPTANCE_CONFIG_PINNED_REVISION"
SNAPSHOT_DIR_ENV = "NAHLA_STAGING_ACCEPTANCE_CONFIG_SNAPSHOT_DIR"

# ── ARCH-001 guard (do not disturb during shadow) ─────────────────────────────
ARCH001_SHADOW_MODE_ENV = "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"
ARCH001_SHADOW_ACTIVE_VALUE = "shadow"
ARCH001_SAFE_VALUES = frozenset({"off", ""})

# ── Closed allowlist: variables that may be copied/moved between staging apps ──
MIGRATABLE_VARIABLE_KEYS: tuple[str, ...] = (
    "BACKEND_URL",
    "D360_API_BASE_URL",
    "D360_PARTNER_HUB_BASE",
    "D360_PARTNER_API_KEY",
    "D360_PARTNER_ID",
    "D360_WEBHOOK_INTERNAL_SECRET",
    "META_APP_SECRET",
    "META_WEBHOOK_ENFORCE_SIGNATURE",
    "META_WEBHOOK_ALLOW_MISSING_SIGNATURE",
    "WHATSAPP_API_URL",
    "WHATSAPP_TOKEN",
    "WHATSAPP_VERIFY_TOKEN",
)

# DB/Redis only when value is a Railway reference to canonical staging bindings.
REFERENCE_BINDABLE_KEYS: tuple[str, ...] = (
    "DATABASE_URL",
    "REDIS_URL",
)

ALLOWED_STAGING_DATABASE_REFERENCE_MARKERS: tuple[str, ...] = (
    "postgres-staging",
    "${{postgres-staging",
    "${{nahla-postgres-prod-staging",
)
ALLOWED_STAGING_REDIS_REFERENCE_MARKERS: tuple[str, ...] = (
    "redis-staging",
    "${{redis-staging",
)

# Never touched by consolidation apply (ARCH-001 and acceptance runtime gates).
PROTECTED_VARIABLE_KEYS: frozenset[str] = frozenset(
    {
        ARCH001_SHADOW_MODE_ENV,
        "NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED",
        "NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM",
        "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE",
    }
)

# Signature verification must never be weakened during consolidation.
SIGNATURE_MODE_KEYS: tuple[str, ...] = (
    "META_WEBHOOK_ENFORCE_SIGNATURE",
    "META_WEBHOOK_ALLOW_MISSING_SIGNATURE",
)
FORBIDDEN_SIGNATURE_WEAKENING: dict[str, str] = {
    "META_WEBHOOK_ENFORCE_SIGNATURE": "false",
    "META_WEBHOOK_ALLOW_MISSING_SIGNATURE": "true",
}

# Closed acceptance target path — Meta Cloud API direct only.
# 360dialog is legacy/transition observability; it must not satisfy readiness.
ACCEPTANCE_TARGET_PROVIDER_PATH = "meta_cloud_api_direct"
LEGACY_PROVIDER_PATH = "360dialog_legacy_transition_only"

# Closed target onboarding — per-merchant Meta Embedded Signup (merchant-owned assets).
META_ONBOARDING_TARGET_PATH = "meta_embedded_signup_per_merchant"
META_ONBOARDING_MERCHANT_OWNED_ASSETS: tuple[str, ...] = (
    "waba_id",
    "phone_number_id",
    "access_token",
)
META_ONBOARDING_EXTERNAL_BLOCKER = "meta_business_verification"

# Pre-verification acceptance only: Tenant 1 direct-Meta test channel cutover.
# Must remain reversible, acceptance-only, and must not unlock production.
ACCEPTANCE_CUTOVER_LABEL = "acceptance_only_not_production"
ACCEPTANCE_CUTOVER_SCOPE = "tenant_1_preverification_direct_meta_test_channel"
TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID = 1
ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS: tuple[str, ...] = (
    "meta_webhook_target",
    "staging_env_secrets_fingerprints",
    "staging_db_wa_connection_binding",
)
ACCEPTANCE_CUTOVER_FORBIDDEN_UNLOCKS: frozenset[str] = frozenset(
    {"production", "runtime_abstraction", "permanent_staging_waba"}
)

# Channel readiness — report absent names; never claim READY without these present.
CHANNEL_READINESS_REQUIRED_KEYS: tuple[str, ...] = (
    "META_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_TOKEN",
    "BACKEND_URL",
)
# Legacy D360 keys may remain migratable but never count toward acceptance readiness.
D360_LEGACY_DETECTION_KEYS: tuple[str, ...] = (
    "D360_API_BASE_URL",
    "D360_PARTNER_ID",
)
# Backward alias — do not use for readiness gaps.
D360_READINESS_KEYS = D360_LEGACY_DETECTION_KEYS

META_DIRECT_WEBHOOK_ROUTE = "/webhook/whatsapp"
# Used only for Tenant 1 acceptance-only cutover attestation — not a permanent staging WABA.
ACCEPTANCE_CUTOVER_DB_BINDING_FIELDS: tuple[str, ...] = (
    "waba_id",
    "phone_number_id",
    "tenant_id",
)
ACCEPTANCE_CUTOVER_DB_BINDING_PROVIDER_VALUE = "meta"
# Backward aliases — prefer ACCEPTANCE_CUTOVER_* names.
STAGING_DB_WA_BINDING_REQUIRED_FIELDS = ACCEPTANCE_CUTOVER_DB_BINDING_FIELDS
STAGING_DB_WA_BINDING_PROVIDER_VALUE = ACCEPTANCE_CUTOVER_DB_BINDING_PROVIDER_VALUE

# ── Phases ───────────────────────────────────────────────────────────────────
PHASE_DEFAULT_OFF = "default_off"
PHASE_STAGING_IDENTITY = "staging_identity"
PHASE_RAILWAY_ALLOWLIST = "railway_allowlist"
PHASE_ARCH001_SHADOW_BLOCK = "arch001_shadow_block"
PHASE_ARCH001_TEARDOWN_PROOF = "arch001_teardown_proof"
PHASE_INVENTORY = "inventory"
PHASE_DRY_RUN_PLAN = "dry_run_plan"
PHASE_CONFLICT_DETECTION = "conflict_detection"
PHASE_SNAPSHOT = "snapshot"
PHASE_APPLY = "apply"
PHASE_VERIFY = "verify_post_apply"
PHASE_ROLLBACK = "rollback"
PHASE_ROUTING_SELECTION = "routing_selection"
PHASE_SUMMARY = "summary"

# ── Failure codes ────────────────────────────────────────────────────────────
CODE_COMMAND_INVALID = "command_invalid"
CODE_PROBE_FAILED = "probe_failed"
CODE_CONSOLIDATION_NOT_ENABLED = "consolidation_not_enabled"
CODE_APPLY_NOT_CONFIRMED = "apply_not_confirmed"
CODE_STAGING_IDENTITY_REJECTED = "staging_identity_rejected"
CODE_RAILWAY_ALLOWLIST_REJECTED = "railway_allowlist_rejected"
CODE_PRODUCTION_REJECTED = "production_rejected"
CODE_ARCH001_SHADOW_ACTIVE = "arch001_shadow_active"
CODE_ARCH001_TEARDOWN_PROOF_MISSING = "arch001_teardown_proof_missing"
CODE_ARCH001_SIGNOFF_MISSING = "arch001_shadow_signoff_missing"
CODE_CONFLICT_DETECTED = "conflict_detected"
CODE_SNAPSHOT_KEY_MISSING = "snapshot_key_missing"
CODE_SNAPSHOT_INVALID = "snapshot_invalid"
CODE_PROTECTED_VARIABLE_TOUCH = "protected_variable_touch"
CODE_SIGNATURE_WEAKENING = "signature_weakening"
CODE_REFERENCE_BINDING_REJECTED = "reference_binding_rejected"
CODE_CHANNEL_READINESS_GAP = "channel_readiness_gap"
CODE_CHANNEL_D360_ONLY_LEGACY_PATH = "channel_d360_only_legacy_path"
CODE_RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"
CODE_DEPLOY_ATTESTATION_FAILED = "deploy_attestation_failed"
CODE_SECRET_LEAKAGE = "secret_leakage"

_FINGERPRINT_SALT = "nahla-staging-acceptance-consolidation-v1"
_VALUE_LIKE_RE = re.compile(
    r"(?i)(secret|token|password|key|api_key|database_url|redis_url|authorization)"
)
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SENTINEL_UUID_PREFIXES = ("00000000-", "11111111-", "ffffffff-")


def env_flag_enabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY_ENV


def fingerprint_value(value: str, *, key: str, hmac_key: str) -> str:
    """Keyed HMAC fingerprint — equality check without exposing the value."""
    if not hmac_key:
        raise ValueError("hmac_key_missing")
    material = f"{key}\0{value}"
    digest = hmac.new(hmac_key.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest[:32]}"


def presence_only(variables: Mapping[str, str], keys: tuple[str, ...] | frozenset[str]) -> dict[str, str]:
    return {name: "present" if (variables.get(name) or "").strip() else "absent" for name in keys}


def is_staging_database_reference(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in ALLOWED_STAGING_DATABASE_REFERENCE_MARKERS)


def is_staging_redis_reference(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in ALLOWED_STAGING_REDIS_REFERENCE_MARKERS)


def is_reference_bindable(key: str, value: str) -> bool:
    if key == "DATABASE_URL":
        return is_staging_database_reference(value)
    if key == "REDIS_URL":
        return is_staging_redis_reference(value)
    return False


def all_migratable_keys() -> tuple[str, ...]:
    return MIGRATABLE_VARIABLE_KEYS + REFERENCE_BINDABLE_KEYS


def is_placeholder_or_sentinel_uuid(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return (
        not _UUID_RE.fullmatch(normalized)
        or normalized.startswith(_SENTINEL_UUID_PREFIXES)
    )


def validate_pinned_identity_contract() -> str | None:
    pinned = (
        STAGING_RAILWAY_PROJECT_ID,
        STAGING_RAILWAY_ENVIRONMENT_ID,
        CANONICAL_SERVICE_ID,
        LEGACY_SOURCE_SERVICE_ID,
    )
    if any(is_placeholder_or_sentinel_uuid(value) for value in pinned):
        return "placeholder_or_sentinel_uuid"
    if len(set(pinned)) != len(pinned):
        return "pinned_identity_ids_not_distinct"
    if CANONICAL_SERVICE_NAME == LEGACY_SOURCE_SERVICE_NAME:
        return "service_names_not_distinct"
    if set(STAGING_SERVICE_ALLOWLIST) != {
        CANONICAL_SERVICE_NAME,
        LEGACY_SOURCE_SERVICE_NAME,
    }:
        return "service_allowlist_names_invalid"
    if STAGING_SERVICE_ALLOWLIST[CANONICAL_SERVICE_NAME] != CANONICAL_SERVICE_ID:
        return "canonical_service_mapping_invalid"
    if STAGING_SERVICE_ALLOWLIST[LEGACY_SOURCE_SERVICE_NAME] != LEGACY_SOURCE_SERVICE_ID:
        return "legacy_service_mapping_invalid"
    return None


def validate_readonly_inventory_identity(inventory: Mapping[str, Any]) -> str | None:
    """Validate the pinned IDs against Railway ``list --json`` project schema."""
    if inventory.get("id") != STAGING_RAILWAY_PROJECT_ID:
        return "inventory_project_id_mismatch"
    if inventory.get("name") != STAGING_PROJECT_VALUE:
        return "inventory_project_name_mismatch"

    environments = inventory.get("environments")
    if not isinstance(environments, Mapping):
        return "inventory_environments_invalid"
    environment_edges = environments.get("edges")
    if not isinstance(environment_edges, list):
        return "inventory_environment_edges_invalid"

    staging_node: Mapping[str, Any] | None = None
    for edge in environment_edges:
        if not isinstance(edge, Mapping) or not isinstance(edge.get("node"), Mapping):
            return "inventory_environment_edge_invalid"
        node = edge["node"]
        if node.get("id") in PRODUCTION_RAILWAY_ENVIRONMENT_IDS:
            if node.get("name") != "production":
                return "production_environment_identity_drift"
        if node.get("name") == STAGING_ENVIRONMENT_VALUE:
            staging_node = node
    if staging_node is None:
        return "inventory_staging_environment_missing"
    if staging_node.get("id") != STAGING_RAILWAY_ENVIRONMENT_ID:
        return "inventory_staging_environment_id_mismatch"

    instances = staging_node.get("serviceInstances")
    if not isinstance(instances, Mapping) or not isinstance(instances.get("edges"), list):
        return "inventory_staging_instances_invalid"
    staging_service_ids = {
        edge.get("node", {}).get("serviceId")
        for edge in instances["edges"]
        if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping)
    }
    if not {CANONICAL_SERVICE_ID, LEGACY_SOURCE_SERVICE_ID} <= staging_service_ids:
        return "inventory_staging_service_relationship_mismatch"

    services = inventory.get("services")
    if not isinstance(services, Mapping) or not isinstance(services.get("edges"), list):
        return "inventory_services_invalid"
    names_by_id: dict[str, str] = {}
    for edge in services["edges"]:
        if not isinstance(edge, Mapping) or not isinstance(edge.get("node"), Mapping):
            return "inventory_service_edge_invalid"
        node = edge["node"]
        service_id = node.get("id")
        service_name = node.get("name")
        if isinstance(service_id, str) and isinstance(service_name, str):
            names_by_id[service_id] = service_name
    if names_by_id.get(CANONICAL_SERVICE_ID) != CANONICAL_SERVICE_NAME:
        return "inventory_canonical_service_name_mismatch"
    if names_by_id.get(LEGACY_SOURCE_SERVICE_ID) != LEGACY_SOURCE_SERVICE_NAME:
        return "inventory_legacy_service_name_mismatch"
    return validate_pinned_identity_contract()


def validate_railway_identity(
    *,
    project_id: str,
    environment_id: str,
    service_id: str,
    service_name: str,
) -> str | None:
    contract_failure = validate_pinned_identity_contract()
    if contract_failure is not None:
        return contract_failure
    if project_id != STAGING_RAILWAY_PROJECT_ID:
        return "project_id_not_allowlisted"
    if environment_id in PRODUCTION_RAILWAY_ENVIRONMENT_IDS:
        return "production_environment_id_forbidden"
    if environment_id != STAGING_RAILWAY_ENVIRONMENT_ID:
        return "environment_id_not_allowlisted"
    expected_id = STAGING_SERVICE_ALLOWLIST.get(service_name)
    if expected_id is None:
        return "service_name_not_allowlisted"
    if service_id != expected_id:
        return "service_id_not_allowlisted"
    if service_name in FORBIDDEN_SERVICE_NAMES:
        return "service_name_forbidden"
    return None


def detect_conflicts(
    *,
    source_vars: Mapping[str, str],
    dest_vars: Mapping[str, str],
    hmac_key: str,
    keys: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    for key in keys or all_migratable_keys():
        src = (source_vars.get(key) or "").strip()
        dst = (dest_vars.get(key) or "").strip()
        if not src or not dst:
            continue
        src_fp = fingerprint_value(src, key=key, hmac_key=hmac_key)
        dst_fp = fingerprint_value(dst, key=key, hmac_key=hmac_key)
        if src_fp != dst_fp:
            conflicts.append(
                {
                    "key": key,
                    "source_fingerprint": src_fp,
                    "dest_fingerprint": dst_fp,
                    "resolution": "fail_closed_manual_review",
                }
            )
    return conflicts


def build_migration_plan(
    *,
    source_vars: Mapping[str, str],
    dest_vars: Mapping[str, str],
) -> dict[str, Any]:
    copy_from_source: list[str] = []
    keep_dest: list[str] = []
    absent_both: list[str] = []
    for key in all_migratable_keys():
        src = (source_vars.get(key) or "").strip()
        dst = (dest_vars.get(key) or "").strip()
        if key in REFERENCE_BINDABLE_KEYS:
            if src and is_reference_bindable(key, src) and not dst:
                copy_from_source.append(key)
            elif dst:
                keep_dest.append(key)
            else:
                absent_both.append(key)
            continue
        if src and not dst:
            copy_from_source.append(key)
        elif dst:
            keep_dest.append(key)
        else:
            absent_both.append(key)
    return {
        "canonical_service": CANONICAL_SERVICE_NAME,
        "legacy_source_service": LEGACY_SOURCE_SERVICE_NAME,
        "copy_from_source": copy_from_source,
        "keep_dest": keep_dest,
        "absent_both": absent_both,
        "protected_keys": sorted(PROTECTED_VARIABLE_KEYS),
    }


def _variable_present(variables: Mapping[str, str], key: str) -> bool:
    return bool((variables.get(key) or "").strip())


def is_d360_legacy_present(variables: Mapping[str, str]) -> bool:
    return any(_variable_present(variables, key) for key in D360_LEGACY_DETECTION_KEYS)


def is_meta_credential_complete(variables: Mapping[str, str]) -> bool:
    return all(_variable_present(variables, key) for key in CHANNEL_READINESS_REQUIRED_KEYS)


def is_d360_only_legacy_path(variables: Mapping[str, str]) -> bool:
    """True when D360 legacy vars exist but Meta direct credentials are incomplete."""
    return is_d360_legacy_present(variables) and not is_meta_credential_complete(variables)


def meta_signature_mode_gaps(variables: Mapping[str, str]) -> list[str]:
    gaps: list[str] = []
    if _variable_present(variables, "META_WEBHOOK_ENFORCE_SIGNATURE"):
        if (variables.get("META_WEBHOOK_ENFORCE_SIGNATURE") or "").strip().lower() == "false":
            gaps.append("META_WEBHOOK_ENFORCE_SIGNATURE")
    if _variable_present(variables, "META_WEBHOOK_ALLOW_MISSING_SIGNATURE"):
        if (variables.get("META_WEBHOOK_ALLOW_MISSING_SIGNATURE") or "").strip().lower() == "true":
            gaps.append("META_WEBHOOK_ALLOW_MISSING_SIGNATURE")
    return gaps


def meta_webhook_route_gaps(routes: tuple[str, ...] | list[str] | None) -> list[str]:
    if not routes:
        return ["meta_direct_webhook_route"]
    normalized = {str(route).strip().rstrip("/") or "/" for route in routes}
    if META_DIRECT_WEBHOOK_ROUTE not in normalized:
        return ["meta_direct_webhook_route"]
    return []


def staging_db_wa_binding_gaps(binding: Mapping[str, str] | None) -> list[str]:
    """Acceptance-only cutover binding gaps. None binding is not a gap by default."""
    if binding is None:
        return []
    gaps: list[str] = []
    for field in ACCEPTANCE_CUTOVER_DB_BINDING_FIELDS:
        if not str(binding.get(field) or "").strip():
            gaps.append(f"acceptance_cutover_db_binding.{field}")
    provider = str(
        binding.get("provider") or ACCEPTANCE_CUTOVER_DB_BINDING_PROVIDER_VALUE
    ).strip().lower()
    if provider != ACCEPTANCE_CUTOVER_DB_BINDING_PROVIDER_VALUE:
        gaps.append("acceptance_cutover_db_binding.provider")
    tenant_id = str(binding.get("tenant_id") or "").strip()
    if tenant_id and tenant_id != str(TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID):
        gaps.append("acceptance_cutover_db_binding.tenant_id")
    return gaps


def acceptance_cutover_snapshot_gaps(snapshot: Mapping[str, Any] | None) -> list[str]:
    if snapshot is None:
        return ["acceptance_cutover_snapshot"]
    gaps: list[str] = []
    if str(snapshot.get("label") or "").strip() != ACCEPTANCE_CUTOVER_LABEL:
        gaps.append("acceptance_cutover_snapshot.label")
    if str(snapshot.get("scope") or "").strip() != ACCEPTANCE_CUTOVER_SCOPE:
        gaps.append("acceptance_cutover_snapshot.scope")
    for component in ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS:
        if component not in snapshot:
            gaps.append(f"acceptance_cutover_snapshot.{component}")
    unlocks = snapshot.get("forbidden_unlocks_respected")
    if unlocks is not True:
        gaps.append("acceptance_cutover_snapshot.forbidden_unlocks_respected")
    return gaps


def channel_readiness_gaps(
    variables: Mapping[str, str],
    *,
    routes: tuple[str, ...] | list[str] | None = None,
    db_binding: Mapping[str, str] | None = None,
    acceptance_cutover_snapshot: Mapping[str, Any] | None = None,
    require_tenant_1_cutover: bool = False,
) -> list[str]:
    """Meta config gaps only. Route readiness requires external attestation."""
    _ = routes  # inventory-only callers may pass observed routes without claiming readiness
    gaps = meta_config_readiness_gaps(variables)
    if require_tenant_1_cutover:
        gaps.extend(acceptance_cutover_snapshot_gaps(acceptance_cutover_snapshot))
        gaps.extend(staging_db_wa_binding_gaps(db_binding))
    return gaps


def meta_config_readiness_gaps(variables: Mapping[str, str]) -> list[str]:
    """Env-key readiness only. D360 absence is never reported as a gap."""
    gaps: list[str] = []
    for key in CHANNEL_READINESS_REQUIRED_KEYS:
        if not _variable_present(variables, key):
            gaps.append(key)
    gaps.extend(meta_signature_mode_gaps(variables))
    return gaps


def evaluate_meta_channel_readiness(
    variables: Mapping[str, str],
    *,
    routes: tuple[str, ...] | list[str] | None = None,
    db_binding: Mapping[str, str] | None = None,
    acceptance_cutover_snapshot: Mapping[str, Any] | None = None,
    require_tenant_1_cutover: bool = False,
) -> dict[str, Any]:
    d360_only = is_d360_only_legacy_path(variables)
    gaps = channel_readiness_gaps(
        variables,
        routes=routes,
        db_binding=db_binding,
        acceptance_cutover_snapshot=acceptance_cutover_snapshot,
        require_tenant_1_cutover=require_tenant_1_cutover,
    )
    meta_config_present = (not d360_only) and not meta_config_readiness_gaps(variables)
    return {
        "acceptance_target_path": ACCEPTANCE_TARGET_PROVIDER_PATH,
        "meta_onboarding_target_path": META_ONBOARDING_TARGET_PATH,
        "meta_onboarding_external_blocker": META_ONBOARDING_EXTERNAL_BLOCKER,
        "legacy_provider_path_excluded": LEGACY_PROVIDER_PATH,
        "d360_only_legacy_path": d360_only,
        "d360_legacy_present": is_d360_legacy_present(variables),
        "meta_credential_complete": is_meta_credential_complete(variables),
        "meta_config_present": meta_config_present,
        "tenant_1_acceptance_cutover_required": require_tenant_1_cutover,
        "acceptance_cutover_label": ACCEPTANCE_CUTOVER_LABEL if require_tenant_1_cutover else None,
        "channel_readiness_gaps": gaps,
        "channel_ready": meta_config_present and not gaps,
    }


def build_acceptance_cutover_guidance() -> dict[str, Any]:
    """Document-only guidance for Tenant 1 pre-verification cutover. No mutations."""
    return {
        "label": ACCEPTANCE_CUTOVER_LABEL,
        "scope": ACCEPTANCE_CUTOVER_SCOPE,
        "tenant_id": TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID,
        "target_onboarding_path": META_ONBOARDING_TARGET_PATH,
        "external_blocker": META_ONBOARDING_EXTERNAL_BLOCKER,
        "merchant_owned_assets": list(META_ONBOARDING_MERCHANT_OWNED_ASSETS),
        "snapshot_components": list(ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS),
        "forbidden_unlocks": sorted(ACCEPTANCE_CUTOVER_FORBIDDEN_UNLOCKS),
        "rollback_required": True,
        "production_unlock": False,
        "runtime_abstraction": False,
        "permanent_staging_waba_dependency": False,
        "operator_note": (
            "Pre-verification acceptance only. Snapshot Meta webhook target, staging "
            "env secret fingerprints, and Tenant 1 whatsapp_connections binding before "
            "cutover; restore from snapshot after acceptance window. Do not perform "
            "cutover in CI or from this governance PR."
        ),
    }


def sanitize_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip value-like strings from operator JSON (defense in depth)."""
    encoded = json.dumps(payload, sort_keys=True)
    if _VALUE_LIKE_RE.search(encoded):
        raise ValueError(CODE_SECRET_LEAKAGE)
    return dict(payload)


__all__ = [
    "ACCEPTANCE_CUTOVER_DB_BINDING_FIELDS",
    "ACCEPTANCE_CUTOVER_DB_BINDING_PROVIDER_VALUE",
    "ACCEPTANCE_CUTOVER_FORBIDDEN_UNLOCKS",
    "ACCEPTANCE_CUTOVER_LABEL",
    "ACCEPTANCE_CUTOVER_SCOPE",
    "ACCEPTANCE_CUTOVER_SNAPSHOT_COMPONENTS",
    "ACCEPTANCE_TARGET_PROVIDER_PATH",
    "ALLOWED_STAGING_DATABASE_REFERENCE_MARKERS",
    "ALLOWED_STAGING_REDIS_REFERENCE_MARKERS",
    "APPLY_CONFIRM_ENV",
    "APPLY_CONFIRM_TOKEN",
    "ARCH001_SAFE_VALUES",
    "ARCH001_SHADOW_ACTIVE_VALUE",
    "ARCH001_SHADOW_MODE_ENV",
    "ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST_ENV",
    "ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID_ENV",
    "ARCH001_PREPROD_ISOLATED_SERVICE_ID_ENV",
    "ARCH001_PREPROD_ISOLATED_SERVICE_NAME_ENV",
    "ARCH001_PREPROD_PINNED_REVISION_ENV",
    "ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV",
    "ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV",
    "ARCH001_SHADOW_SIGNOFF_ENV",
    "ARCH001_TEARDOWN_PROOF_ENV",
    "CANONICAL_SERVICE_ID",
    "CANONICAL_SERVICE_NAME",
    "CHANNEL_READINESS_REQUIRED_KEYS",
    "CODE_APPLY_NOT_CONFIRMED",
    "CODE_ARCH001_SHADOW_ACTIVE",
    "CODE_ARCH001_SIGNOFF_MISSING",
    "CODE_ARCH001_TEARDOWN_PROOF_MISSING",
    "CODE_CHANNEL_D360_ONLY_LEGACY_PATH",
    "CODE_CHANNEL_READINESS_GAP",
    "CODE_COMMAND_INVALID",
    "CODE_CONFLICT_DETECTED",
    "CODE_CONSOLIDATION_NOT_ENABLED",
    "CODE_DEPLOY_ATTESTATION_FAILED",
    "CODE_PROBE_FAILED",
    "CODE_PROTECTED_VARIABLE_TOUCH",
    "CODE_PRODUCTION_REJECTED",
    "CODE_RAILWAY_ALLOWLIST_REJECTED",
    "CODE_REFERENCE_BINDING_REJECTED",
    "CODE_RUNTIME_REVISION_MISMATCH",
    "CODE_SECRET_LEAKAGE",
    "CODE_SIGNATURE_WEAKENING",
    "CODE_SNAPSHOT_INVALID",
    "CODE_SNAPSHOT_KEY_MISSING",
    "CODE_STAGING_IDENTITY_REJECTED",
    "D360_LEGACY_DETECTION_KEYS",
    "D360_READINESS_KEYS",
    "evaluate_meta_channel_readiness",
    "FORBIDDEN_ENVIRONMENT_MARKERS",
    "FORBIDDEN_SERVICE_NAMES",
    "FORBIDDEN_SIGNATURE_WEAKENING",
    "is_d360_legacy_present",
    "is_d360_only_legacy_path",
    "is_meta_credential_complete",
    "LEGACY_PROVIDER_PATH",
    "all_migratable_keys",
    "META_ONBOARDING_EXTERNAL_BLOCKER",
    "META_ONBOARDING_MERCHANT_OWNED_ASSETS",
    "META_ONBOARDING_TARGET_PATH",
    "LEGACY_SOURCE_SERVICE_ID",
    "LEGACY_SOURCE_SERVICE_NAME",
    "meta_config_readiness_gaps",
    "meta_signature_mode_gaps",
    "meta_webhook_route_gaps",
    "staging_db_wa_binding_gaps",
    "META_DIRECT_WEBHOOK_ROUTE",
    "TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID",
    "STAGING_DB_WA_BINDING_REQUIRED_FIELDS",
    "MASTER_ENABLE_ENV",
    "MIGRATABLE_VARIABLE_KEYS",
    "PHASE_APPLY",
    "PHASE_ARCH001_SHADOW_BLOCK",
    "PHASE_ARCH001_TEARDOWN_PROOF",
    "PHASE_CONFLICT_DETECTION",
    "PHASE_DEFAULT_OFF",
    "PHASE_DRY_RUN_PLAN",
    "PHASE_INVENTORY",
    "PHASE_RAILWAY_ALLOWLIST",
    "PHASE_ROLLBACK",
    "PHASE_ROUTING_SELECTION",
    "PHASE_SNAPSHOT",
    "PHASE_STAGING_IDENTITY",
    "PHASE_SUMMARY",
    "PHASE_VERIFY",
    "PINNED_REVISION_ENV",
    "PROTECTED_VARIABLE_KEYS",
    "PRODUCTION_RAILWAY_ENVIRONMENT_IDS",
    "REFERENCE_BINDABLE_KEYS",
    "REPORT_SCHEMA_VERSION",
    "SIGNATURE_MODE_KEYS",
    "SNAPSHOT_DIR_ENV",
    "SNAPSHOT_KEY_ENV",
    "SNAPSHOT_SCHEMA_VERSION",
    "STAGING_ENVIRONMENT_ENV",
    "STAGING_ENVIRONMENT_ID_ENV",
    "STAGING_ENVIRONMENT_VALUE",
    "STAGING_IDENTITY_CLASS",
    "STAGING_PROJECT_ENV",
    "STAGING_PROJECT_ID_ENV",
    "STAGING_PROJECT_VALUE",
    "STAGING_RAILWAY_ENVIRONMENT_ID",
    "STAGING_RAILWAY_PROJECT_ID",
    "STAGING_SERVICE_ALLOWLIST",
    "acceptance_cutover_snapshot_gaps",
    "build_acceptance_cutover_guidance",
    "build_migration_plan",
    "channel_readiness_gaps",
    "detect_conflicts",
    "env_flag_enabled",
    "fingerprint_value",
    "is_reference_bindable",
    "is_placeholder_or_sentinel_uuid",
    "presence_only",
    "sanitize_report_payload",
    "validate_railway_identity",
    "validate_pinned_identity_contract",
    "validate_readonly_inventory_identity",
]
