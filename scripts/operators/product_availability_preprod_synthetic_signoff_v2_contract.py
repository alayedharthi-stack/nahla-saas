"""Closed contract for ARCH-001 preprod synthetic signoff v2.

Replaces the zero-traffic 48h observation-window prerequisite with a
phase/lifecycle-based synthetic matrix signoff. Organic traffic is **not**
claimed; post-approval canonical shadow during limited allowlisted canary and
enforce eligibility remain separate pending gates.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

INITIATIVE_ID = "ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2"
BUNDLE_SCHEMA_VERSION = "product_availability_preprod_synthetic_signoff_v2"
LEGACY_V1_SCHEMA_VERSION = "product_availability_shadow_staging_signoff_v1"
MATRIX_REPORT_SCHEMA_VERSION = "product_availability_shadow_observation_v1"

TRAFFIC_CLAIM = "synthetic_probes_only"
POST_APPROVAL_PENDING = "pending"

SHADOW_MODE_ENV = "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"
SHADOW_MODE_VALUE = "shadow"
ENFORCE_MODE_VALUE = "enforce"
DEPLOYMENT_APP_ROOT = "/app"

SIGNOFF_ARTIFACT_ENV = "NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT"
SIGNOFF_HMAC_KEY_ENV = "NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY"

CANONICAL_SERVICE_NAME = "nahla-saas"
CANONICAL_SERVICE_ID = "686b36c5-a926-4e58-912a-5e9d13fbc2e7"

# ── Lifecycle phases (6 total) ───────────────────────────────────────────────
PHASE_BASELINE = "baseline"
PHASE_CONTAINER_RESTART = "container_restart"
PHASE_FRESH_PINNED_REDEPLOY = "fresh_pinned_redeploy"
PHASE_REPEAT_MATRIX_1 = "repeat_matrix_1"
PHASE_REPEAT_MATRIX_2 = "repeat_matrix_2"
PHASE_REPEAT_MATRIX_3 = "repeat_matrix_3"

LIFECYCLE_PHASES: tuple[str, ...] = (
    PHASE_BASELINE,
    PHASE_CONTAINER_RESTART,
    PHASE_FRESH_PINNED_REDEPLOY,
    PHASE_REPEAT_MATRIX_1,
    PHASE_REPEAT_MATRIX_2,
    PHASE_REPEAT_MATRIX_3,
)

PHASE_NEGATIVE_CONTROLS = "negative_controls"
PHASE_BUNDLE = "bundle"
PHASE_VERIFY = "verify"
PHASE_LEGACY_V1_READ = "legacy_v1_read"

# ── Required 7/7 synthetic case matrix ───────────────────────────────────────
REQUIRED_CASE_IDS: frozenset[str] = frozenset(
    {
        "catalog_available_positive_claim",
        "catalog_unavailable_negative_claim",
        "irrelevant_turn_no_claim",
        "kb_catalog_conflict",
        "tenant_b_isolation",
        "unknown_entity_positive_claim",
        "variant_specific_conflict",
    }
)

# ── Safety invariants (zero tolerance) ───────────────────────────────────────
MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES = 0
MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS = 0
MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS = 0
MAX_ACCEPTABLE_OUTBOUND_PROVIDER_CALLS = 0

# ── Negative control IDs (must BLOCK) ─────────────────────────────────────────
NEGATIVE_WRONG_MANIFEST = "wrong_manifest"
NEGATIVE_WRONG_REVISION = "wrong_revision"
NEGATIVE_OUTSIDE_APP = "outside_app"
NEGATIVE_ENFORCE_ENABLED = "enforce_enabled"

NEGATIVE_CONTROL_IDS: frozenset[str] = frozenset(
    {
        NEGATIVE_WRONG_MANIFEST,
        NEGATIVE_WRONG_REVISION,
        NEGATIVE_OUTSIDE_APP,
        NEGATIVE_ENFORCE_ENABLED,
    }
)

NEGATIVE_CONTROL_EXPECTED_CODES: dict[str, str] = {
    NEGATIVE_WRONG_MANIFEST: "artifact_manifest_mismatch",
    NEGATIVE_WRONG_REVISION: "runtime_revision_mismatch",
    NEGATIVE_OUTSIDE_APP: "runtime_execution_required",
    NEGATIVE_ENFORCE_ENABLED: "enforce_mode_enabled",
}

# ── Dependency fault taxonomy ─────────────────────────────────────────────────
DEPENDENCY_FAULT_SKIPPED = "skipped_not_supported"

# ── Failure codes ─────────────────────────────────────────────────────────────
CODE_COMMAND_INVALID = "command_invalid"
CODE_PROBE_FAILED = "probe_failed"
CODE_BUNDLE_INVALID = "bundle_invalid"
CODE_BUNDLE_SIGNATURE_INVALID = "bundle_signature_invalid"
CODE_LEGACY_V1_NOT_SUFFICIENT = "legacy_v1_not_sufficient_for_preprod"
CODE_LIFECYCLE_PHASE_MISSING = "lifecycle_phase_missing"
CODE_LIFECYCLE_PHASE_FAILED = "lifecycle_phase_failed"
CODE_MATRIX_CASE_MISSING = "matrix_case_missing"
CODE_MATRIX_INVARIANT_VIOLATION = "matrix_invariant_violation"
CODE_NEGATIVE_CONTROL_MISSING = "negative_control_missing"
CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS = "negative_control_unexpected_pass"
CODE_IDENTITY_BINDING_MISMATCH = "identity_binding_mismatch"
CODE_STABLE_COUNTERS_DRIFT = "stable_counters_drift"
CODE_TRAFFIC_CLAIM_INVALID = "traffic_claim_invalid"
CODE_POST_APPROVAL_NOT_PENDING = "post_approval_not_pending"
CODE_TEARDOWN_PROOF_MISSING = "teardown_proof_missing"
CODE_SUPERSEDED_WINDOW_ACTIVE = "superseded_window_active"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

BUNDLE_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "bundle_schema_version",
        "initiative_id",
        "traffic_claim",
        "identity_binding",
        "lifecycle_phases",
        "negative_controls",
        "stable_counter_reference",
        "post_approval",
        "superseded_invalid_windows",
        "teardown_proof",
        "signed_at_utc",
    }
)

IDENTITY_BINDING_KEYS: frozenset[str] = frozenset(
    {
        "pinned_target_revision",
        "manifest_digest",
        "service_name",
        "service_id",
        "deployment_id",
        "image_digest",
    }
)


def is_legacy_v1_bundle(payload: Mapping[str, Any]) -> bool:
    version = str(payload.get("archive_schema_version") or payload.get("bundle_schema_version") or "")
    return version == LEGACY_V1_SCHEMA_VERSION


def validate_identity_binding(binding: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not isinstance(binding, Mapping):
        return [CODE_IDENTITY_BINDING_MISMATCH]
    missing = IDENTITY_BINDING_KEYS - set(binding)
    if missing:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    revision = str(binding.get("pinned_target_revision") or "").strip().lower()
    if not _REVISION_RE.fullmatch(revision):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    digest = str(binding.get("manifest_digest") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if binding.get("service_name") != CANONICAL_SERVICE_NAME:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if binding.get("service_id") != CANONICAL_SERVICE_ID:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    deployment_id = str(binding.get("deployment_id") or "").strip()
    if not _UUID_RE.fullmatch(deployment_id):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    image_digest = str(binding.get("image_digest") or "").strip().lower()
    if image_digest and image_digest != "absent" and not _SHA256_RE.fullmatch(image_digest):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    return blockers


def validate_lifecycle_phase_row(row: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    phase = str(row.get("phase") or "")
    if phase not in LIFECYCLE_PHASES:
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
        return blockers
    if row.get("ok") is not True:
        blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
    matrix = row.get("matrix")
    if not isinstance(matrix, Mapping) or matrix.get("ok") is not True:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        return blockers
    case_results = matrix.get("case_results")
    if not isinstance(case_results, list):
        blockers.append(CODE_MATRIX_CASE_MISSING)
        return blockers
    case_ids = {str(item.get("case_id") or "") for item in case_results if isinstance(item, Mapping)}
    if case_ids != REQUIRED_CASE_IDS:
        blockers.append(CODE_MATRIX_CASE_MISSING)
    guards = matrix.get("guards")
    if not isinstance(guards, Mapping):
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        return blockers
    if guards.get("customer_text_changed_count", 1) != 0:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    if guards.get("additional_llm_calls", 1) != 0:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    if guards.get("duplicate_invocation_count", 1) != 0:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    if guards.get("outbound_provider_calls", 1) != 0:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    dependency_fault = row.get("dependency_fault")
    if dependency_fault is not None:
        if not isinstance(dependency_fault, Mapping):
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif dependency_fault.get("status") != DEPENDENCY_FAULT_SKIPPED:
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif not str(dependency_fault.get("residual_risk") or "").strip():
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
    return blockers


def extract_stable_counters(matrix: Mapping[str, Any]) -> dict[str, int]:
    metrics = matrix.get("metrics") if isinstance(matrix.get("metrics"), Mapping) else {}
    guards = matrix.get("guards") if isinstance(matrix.get("guards"), Mapping) else {}
    return {
        "would_rewrite_count": int(metrics.get("would_rewrite_count") or 0),
        "evaluated_turns": int(metrics.get("evaluated_turns") or 0),
        "customer_text_changed_count": int(guards.get("customer_text_changed_count") or 0),
        "additional_llm_calls": int(guards.get("additional_llm_calls") or 0),
        "duplicate_invocation_count": int(guards.get("duplicate_invocation_count") or 0),
        "outbound_provider_calls": int(guards.get("outbound_provider_calls") or 0),
    }


__all__ = [
    "BUNDLE_REQUIRED_TOP_LEVEL_KEYS",
    "BUNDLE_SCHEMA_VERSION",
    "CANONICAL_SERVICE_ID",
    "CANONICAL_SERVICE_NAME",
    "CODE_BUNDLE_INVALID",
    "CODE_BUNDLE_SIGNATURE_INVALID",
    "CODE_COMMAND_INVALID",
    "CODE_IDENTITY_BINDING_MISMATCH",
    "CODE_LEGACY_V1_NOT_SUFFICIENT",
    "CODE_LIFECYCLE_PHASE_FAILED",
    "CODE_LIFECYCLE_PHASE_MISSING",
    "CODE_MATRIX_CASE_MISSING",
    "CODE_MATRIX_INVARIANT_VIOLATION",
    "CODE_NEGATIVE_CONTROL_MISSING",
    "CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS",
    "CODE_POST_APPROVAL_NOT_PENDING",
    "CODE_PROBE_FAILED",
    "CODE_STABLE_COUNTERS_DRIFT",
    "CODE_SUPERSEDED_WINDOW_ACTIVE",
    "CODE_TEARDOWN_PROOF_MISSING",
    "CODE_TRAFFIC_CLAIM_INVALID",
    "DEPENDENCY_FAULT_SKIPPED",
    "DEPLOYMENT_APP_ROOT",
    "ENFORCE_MODE_VALUE",
    "IDENTITY_BINDING_KEYS",
    "INITIATIVE_ID",
    "LEGACY_V1_SCHEMA_VERSION",
    "LIFECYCLE_PHASES",
    "MATRIX_REPORT_SCHEMA_VERSION",
    "MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS",
    "MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES",
    "MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS",
    "MAX_ACCEPTABLE_OUTBOUND_PROVIDER_CALLS",
    "NEGATIVE_CONTROL_EXPECTED_CODES",
    "NEGATIVE_CONTROL_IDS",
    "NEGATIVE_ENFORCE_ENABLED",
    "NEGATIVE_OUTSIDE_APP",
    "NEGATIVE_WRONG_MANIFEST",
    "NEGATIVE_WRONG_REVISION",
    "PHASE_BASELINE",
    "PHASE_BUNDLE",
    "PHASE_CONTAINER_RESTART",
    "PHASE_FRESH_PINNED_REDEPLOY",
    "PHASE_LEGACY_V1_READ",
    "PHASE_NEGATIVE_CONTROLS",
    "PHASE_REPEAT_MATRIX_1",
    "PHASE_REPEAT_MATRIX_2",
    "PHASE_REPEAT_MATRIX_3",
    "PHASE_VERIFY",
    "POST_APPROVAL_PENDING",
    "REQUIRED_CASE_IDS",
    "SHADOW_MODE_ENV",
    "SHADOW_MODE_VALUE",
    "SIGNOFF_ARTIFACT_ENV",
    "SIGNOFF_HMAC_KEY_ENV",
    "TRAFFIC_CLAIM",
    "extract_stable_counters",
    "is_legacy_v1_bundle",
    "validate_identity_binding",
    "validate_lifecycle_phase_row",
]
