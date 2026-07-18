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

# Channel readiness — report absent names; never claim READY without these present.
CHANNEL_READINESS_REQUIRED_KEYS: tuple[str, ...] = (
    "META_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_TOKEN",
    "BACKEND_URL",
)
D360_READINESS_KEYS: tuple[str, ...] = (
    "D360_API_BASE_URL",
    "D360_PARTNER_ID",
)

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


def channel_readiness_gaps(variables: Mapping[str, str]) -> list[str]:
    gaps: list[str] = []
    for key in CHANNEL_READINESS_REQUIRED_KEYS:
        if not (variables.get(key) or "").strip():
            gaps.append(key)
    d360_present = any((variables.get(key) or "").strip() for key in D360_READINESS_KEYS)
    if not d360_present:
        gaps.extend(list(D360_READINESS_KEYS))
    return gaps


def sanitize_report_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip value-like strings from operator JSON (defense in depth)."""
    encoded = json.dumps(payload, sort_keys=True)
    if _VALUE_LIKE_RE.search(encoded):
        raise ValueError(CODE_SECRET_LEAKAGE)
    return dict(payload)


__all__ = [
    "ALLOWED_STAGING_DATABASE_REFERENCE_MARKERS",
    "ALLOWED_STAGING_REDIS_REFERENCE_MARKERS",
    "APPLY_CONFIRM_ENV",
    "APPLY_CONFIRM_TOKEN",
    "ARCH001_SAFE_VALUES",
    "ARCH001_SHADOW_ACTIVE_VALUE",
    "ARCH001_SHADOW_MODE_ENV",
    "ARCH001_SHADOW_SIGNOFF_ENV",
    "ARCH001_TEARDOWN_PROOF_ENV",
    "CANONICAL_SERVICE_ID",
    "CANONICAL_SERVICE_NAME",
    "CHANNEL_READINESS_REQUIRED_KEYS",
    "CODE_APPLY_NOT_CONFIRMED",
    "CODE_ARCH001_SHADOW_ACTIVE",
    "CODE_ARCH001_SIGNOFF_MISSING",
    "CODE_ARCH001_TEARDOWN_PROOF_MISSING",
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
    "D360_READINESS_KEYS",
    "FORBIDDEN_ENVIRONMENT_MARKERS",
    "FORBIDDEN_SERVICE_NAMES",
    "FORBIDDEN_SIGNATURE_WEAKENING",
    "LEGACY_SOURCE_SERVICE_ID",
    "LEGACY_SOURCE_SERVICE_NAME",
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
    "all_migratable_keys",
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
