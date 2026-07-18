"""Closed contract for staging real-channel acceptance config consolidation.

Consolidates channel/config variables from a legacy staging app service onto the
canonical staging app service within ``desirable-growth`` / ``staging``. Default-off;
no Railway mutations unless all fail-closed gates pass after ARCH-001 shadow teardown.

Railway service IDs are contract-pinned staging allowlist entries. Operators discover
live IDs via ``railway status --json`` and bump this contract in a dedicated PR
before the first apply — never copy production IDs or values.
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

# ── Closed Railway ID allowlist (staging only — bump via PR after discovery) ──
# Discover: railway link --project desirable-growth --environment staging
#           railway status --json
STAGING_RAILWAY_PROJECT_ID = "00000000-0000-4000-8000-000000000001"
STAGING_RAILWAY_ENVIRONMENT_ID = "00000000-0000-4000-8000-000000000002"

CANONICAL_SERVICE_NAME = "nahla-saas"
CANONICAL_SERVICE_ID = "00000000-0000-4000-8000-000000000003"
LEGACY_SOURCE_SERVICE_NAME = "nahla-saas-staging"
LEGACY_SOURCE_SERVICE_ID = "00000000-0000-4000-8000-000000000004"

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


def validate_railway_identity(
    *,
    project_id: str,
    environment_id: str,
    service_id: str,
    service_name: str,
) -> str | None:
    if project_id != STAGING_RAILWAY_PROJECT_ID:
        return "project_id_not_allowlisted"
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
    "REFERENCE_BINDABLE_KEYS",
    "REPORT_SCHEMA_VERSION",
    "SIGNATURE_MODE_KEYS",
    "SNAPSHOT_DIR_ENV",
    "SNAPSHOT_KEY_ENV",
    "SNAPSHOT_SCHEMA_VERSION",
    "STAGING_ENVIRONMENT_ENV",
    "STAGING_ENVIRONMENT_ID",
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
    "presence_only",
    "sanitize_report_payload",
    "validate_railway_identity",
]
