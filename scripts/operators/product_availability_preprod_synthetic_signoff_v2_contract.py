"""Closed contract for ARCH-001 preprod synthetic signoff v2."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from scripts.operators.arch001_container_restart_evidence import (
    RESTART_COLLECTION_METHOD_PROC1_STAT_FIELD22,
    RESTART_EVIDENCE_SNAPSHOT_KEYS,
    RESTART_PROOF_CONTAINER_ID_CHANGE,
    RESTART_PROOF_PID1_STARTTIME_CHANGE,
)

INITIATIVE_ID = "ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2"
BUNDLE_SCHEMA_VERSION = "product_availability_preprod_synthetic_signoff_v2"
PHASE_ARTIFACT_SCHEMA_VERSION = "arch001_preprod_phase_artifact_v1"
NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION = "arch001_preprod_negative_control_v1"
TEARDOWN_PROOF_SCHEMA_VERSION = "arch001_preprod_teardown_v1"
LEGACY_V1_SCHEMA_VERSION = "product_availability_shadow_staging_signoff_v1"
MATRIX_REPORT_SCHEMA_VERSION = "product_availability_shadow_observation_v1"

TRAFFIC_CLAIM = "synthetic_probes_only"
POST_APPROVAL_PENDING = "pending"

EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST = "ci_contract_self_test"
EVIDENCE_CLASS_PRODUCTION_SIGNOFF = "production_signoff"
ELIGIBLE_EVIDENCE_CLASSES = frozenset(
    {EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST, EVIDENCE_CLASS_PRODUCTION_SIGNOFF}
)

SHADOW_MODE_ENV = "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"
SHADOW_MODE_VALUE = "shadow"
ENFORCE_MODE_VALUE = "enforce"
DEPLOYMENT_APP_ROOT = "/app"
EXECUTION_MODE_IN_CONTAINER = "in_container"

SIGNOFF_ARTIFACT_ENV = "NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT"
SIGNOFF_HMAC_KEY_ENV = "NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY"
ISOLATED_SERVICE_NAME_ENV = "NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_NAME"
ISOLATED_SERVICE_ID_ENV = "NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_ID"
ISOLATED_DEPLOYMENT_ID_ENV = "NAHLA_ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID"
EXPECTED_MANIFEST_DIGEST_ENV = "NAHLA_ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST"
EXPECTED_IMAGE_DIGEST_ENV = "NAHLA_ARCH001_PREPROD_EXPECTED_IMAGE_DIGEST"
BASELINE_IMAGE_DIGEST_ENV = "NAHLA_ARCH001_PREPROD_BASELINE_IMAGE_DIGEST"
PINNED_REVISION_ENV = "NAHLA_ARCH001_PREPROD_PINNED_REVISION"
CANONICAL_SERVICE_NAME_ENV = "NAHLA_ARCH001_PREPROD_CANONICAL_SERVICE_NAME"
CANONICAL_SERVICE_ID_ENV = "NAHLA_ARCH001_PREPROD_CANONICAL_SERVICE_ID"
CANONICAL_DEPLOYMENT_ID_ENV = "NAHLA_ARCH001_PREPROD_CANONICAL_DEPLOYMENT_ID"

SERVICE_ROLE_ISOLATED_PREPROD_SHADOW = "isolated_preprod_shadow"
SERVICE_ROLE_CANONICAL_CONTROL = "canonical_control"

HMAC_DOMAIN_PREFIX = "ARCH001_PREPROD_V2\0"
MIN_HMAC_KEY_BYTES = 32
KNOWN_REJECTED_HMAC_KEYS = frozenset(
    {
        "test-hmac-key-for-ci-only",
        "test-arch001-preprod-signoff-v2-hmac",
        "changeme",
        "password",
    }
)

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
PHASE_CONTRACT_SELF_TEST = "contract_self_test"

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

CASE_EXPECT_WOULD_REWRITE: dict[str, bool] = {
    "catalog_available_positive_claim": False,
    "catalog_unavailable_negative_claim": False,
    "irrelevant_turn_no_claim": False,
    "kb_catalog_conflict": True,
    "tenant_b_isolation": False,
    "unknown_entity_positive_claim": True,
    "variant_specific_conflict": False,
}

EXPECTED_STABLE_COUNTERS: dict[str, int] = {
    "evaluated_turns": 7,
    "would_rewrite_count": 2,
    "customer_text_changed_count": 0,
    "additional_llm_calls": 0,
    "duplicate_invocation_count": 0,
    "outbound_provider_calls": 0,
}

REPEAT_MATRIX_MIN_SPACING_SECONDS = 15 * 60
CLOCK_SKEW_ALLOWANCE_SECONDS = 5 * 60
ARTIFACT_MAX_AGE_SECONDS = 14 * 24 * 60 * 60

REQUIRED_SUPERSEDED_WINDOW_IDS: frozenset[str] = frozenset(
    {
        "arch001-48h-zero-traffic-20260718",
        "arch001-48h-zero-traffic-20260720",
    }
)

CASE_EXPECT_GUARD_ACTION: dict[str, str] = {
    "catalog_available_positive_claim": "allowed",
    "catalog_unavailable_negative_claim": "allowed",
    "irrelevant_turn_no_claim": "allowed",
    "kb_catalog_conflict": "rewrite_conflict",
    "tenant_b_isolation": "allowed",
    "unknown_entity_positive_claim": "rewrite_unknown",
    "variant_specific_conflict": "allowed",
}

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

DEPENDENCY_FAULT_SKIPPED = "skipped_not_supported"

SERVICE_STATE_STOPPED = "stopped"
SERVICE_STATE_DOWN = "down"
ALLOWED_ISOLATED_SERVICE_STATES = frozenset({SERVICE_STATE_STOPPED, SERVICE_STATE_DOWN})

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
CODE_TEARDOWN_PROOF_UNVERIFIED = "teardown_proof_unverified"
CODE_SUPERSEDED_WINDOW_ACTIVE = "superseded_window_active"
CODE_SUPERSEDED_WINDOWS_MISSING = "superseded_windows_missing"
CODE_EVIDENCE_CLASS_INELIGIBLE = "evidence_class_ineligible"
CODE_HMAC_KEY_WEAK = "hmac_key_weak"
CODE_PHASE_ARTIFACT_INVALID = "phase_artifact_invalid"
CODE_PHASE_TIMESTAMP_ORDER_INVALID = "phase_timestamp_order_invalid"
CODE_PHASE_DEPLOYMENT_ID_INVALID = "phase_deployment_id_invalid"
CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID = "phase_lifecycle_attestation_invalid"
CODE_EXPECTED_IDENTITY_MISSING = "expected_identity_missing"
CODE_ARCH001_SIGNOFF_MISSING = "arch001_shadow_signoff_missing"
CODE_ARTIFACT_UNREADABLE = "artifact_unreadable"
CODE_RUNTIME_BINDING_MISMATCH = "runtime_binding_mismatch"
CODE_TIMESTAMP_INVALID = "timestamp_invalid"
CODE_TIMESTAMP_STALE = "artifact_timestamp_stale"
CODE_PRODUCTION_SYNTHETIC_MARKER = "production_synthetic_marker_present"
CODE_PHASE_IDENTITY_INCONSISTENT = "phase_identity_inconsistent"
CODE_IMAGE_DIGEST_INCONSISTENT = "image_digest_inconsistent"
CODE_REVISION_FORMAT_INVALID = "revision_format_invalid"
CODE_IMAGE_DIGEST_INVALID = "image_digest_invalid"
CODE_RESTART_EVIDENCE_INVALID = "restart_evidence_invalid"

RESTART_IDENTITY_BINDING_KEYS: frozenset[str] = frozenset(
    {
        "pinned_target_revision",
        "manifest_digest",
        "service_role",
        "service_name",
        "service_id",
        "deployment_id",
        "image_digest",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

IDENTITY_BINDING_KEYS: frozenset[str] = frozenset(
    {
        "pinned_target_revision",
        "manifest_digest",
        "service_role",
        "service_name",
        "service_id",
        "deployment_id",
        "image_digest",
    }
)

ISOLATED_SERVICE_CONSTRAINT_KEYS: frozenset[str] = frozenset(
    {"no_domains", "no_provider_credentials"}
)


def is_legacy_v1_bundle(payload: Mapping[str, Any]) -> bool:
    version = str(payload.get("archive_schema_version") or payload.get("bundle_schema_version") or "")
    return version == LEGACY_V1_SCHEMA_VERSION


def is_strong_hmac_key(key: str, *, allow_fixture_keys: bool = False) -> bool:
    if not key or len(key.encode("utf-8")) < MIN_HMAC_KEY_BYTES:
        return False
    if not allow_fixture_keys and key in KNOWN_REJECTED_HMAC_KEYS:
        return False
    return True


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_positive_int(value: Any) -> int | None:
    parsed = _safe_counter_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def validate_revision_token(value: Any, *, require_full: bool = False) -> bool:
    revision = str(value or "").strip().lower()
    if require_full:
        return bool(_FULL_REVISION_RE.fullmatch(revision))
    return bool(_REVISION_RE.fullmatch(revision))


def validate_image_digest_value(value: Any, *, allow_absent: bool = False) -> bool:
    image_digest = str(value or "").strip().lower()
    if not image_digest:
        return False
    if image_digest == "absent":
        return allow_absent
    return bool(_SHA256_RE.fullmatch(image_digest))


def validate_production_image_digest(value: Any) -> bool:
    return validate_image_digest_value(value, allow_absent=False)


def validate_identity_binding_shape(
    binding: Mapping[str, Any],
    *,
    require_production: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(binding, Mapping):
        return [CODE_IDENTITY_BINDING_MISMATCH]
    missing = IDENTITY_BINDING_KEYS - set(binding)
    if missing:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    revision = str(binding.get("pinned_target_revision") or "").strip().lower()
    if not validate_revision_token(revision, require_full=require_production):
        blockers.append(CODE_REVISION_FORMAT_INVALID if require_production else CODE_IDENTITY_BINDING_MISMATCH)
    digest = str(binding.get("manifest_digest") or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    role = str(binding.get("service_role") or "")
    if role not in {SERVICE_ROLE_ISOLATED_PREPROD_SHADOW, SERVICE_ROLE_CANONICAL_CONTROL}:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if not str(binding.get("service_name") or "").strip():
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if not _UUID_RE.fullmatch(str(binding.get("service_id") or "").strip()):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if not _UUID_RE.fullmatch(str(binding.get("deployment_id") or "").strip()):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    image_digest = str(binding.get("image_digest") or "").strip().lower()
    if require_production:
        if not validate_production_image_digest(image_digest):
            blockers.append(CODE_IMAGE_DIGEST_INVALID)
    elif image_digest and image_digest != "absent" and not _SHA256_RE.fullmatch(image_digest):
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    return blockers


def identity_binding_matches_expected(
    binding: Mapping[str, Any],
    expected: Mapping[str, str],
    *,
    require_production: bool = False,
) -> list[str]:
    blockers = validate_identity_binding_shape(binding, require_production=require_production)
    for key, value in expected.items():
        if str(binding.get(key) or "") != str(value):
            blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    return blockers


def _safe_counter_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
    return None


def safe_extract_stable_counters(matrix: Mapping[str, Any]) -> tuple[dict[str, int] | None, list[str]]:
    blockers: list[str] = []
    if not isinstance(matrix, Mapping):
        return None, [CODE_STABLE_COUNTERS_DRIFT]
    metrics = matrix.get("metrics")
    guards = matrix.get("guards")
    if not isinstance(metrics, Mapping) or not isinstance(guards, Mapping):
        return None, [CODE_STABLE_COUNTERS_DRIFT]
    spec = (
        ("would_rewrite_count", metrics, "would_rewrite_count"),
        ("evaluated_turns", metrics, "evaluated_turns"),
        ("customer_text_changed_count", guards, "customer_text_changed_count"),
        ("additional_llm_calls", guards, "additional_llm_calls"),
        ("duplicate_invocation_count", guards, "duplicate_invocation_count"),
        ("outbound_provider_calls", guards, "outbound_provider_calls"),
    )
    extracted: dict[str, int] = {}
    for key, source, source_key in spec:
        parsed = _safe_counter_int(source.get(source_key))
        if parsed is None:
            blockers.append(CODE_STABLE_COUNTERS_DRIFT)
        else:
            extracted[key] = parsed
    if blockers:
        return None, blockers
    return extracted, []


def extract_stable_counters(matrix: Mapping[str, Any]) -> dict[str, int]:
    extracted, blockers = safe_extract_stable_counters(matrix)
    if blockers or extracted is None:
        return {key: -1 for key in EXPECTED_STABLE_COUNTERS}
    return extracted


def validate_artifact_stable_counters_consistency(
    *,
    matrix: Mapping[str, Any],
    counters: Any,
) -> list[str]:
    extracted, extract_blockers = safe_extract_stable_counters(matrix)
    blockers = list(extract_blockers)
    if not isinstance(counters, Mapping):
        return blockers + [CODE_STABLE_COUNTERS_DRIFT]
    if extracted is None:
        return blockers + [CODE_STABLE_COUNTERS_DRIFT]
    for key, value in extracted.items():
        parsed = _safe_counter_int(counters.get(key))
        if parsed != value:
            blockers.append(CODE_STABLE_COUNTERS_DRIFT)
    return blockers


def validate_matrix_payload(matrix: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if matrix.get("ok") is not True:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    case_results = matrix.get("case_results")
    if not isinstance(case_results, list):
        blockers.append(CODE_MATRIX_CASE_MISSING)
        return blockers
    if len(case_results) != len(REQUIRED_CASE_IDS):
        blockers.append(CODE_MATRIX_CASE_MISSING)
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in case_results:
        if not isinstance(item, Mapping):
            blockers.append(CODE_MATRIX_CASE_MISSING)
            continue
        case_id = str(item.get("case_id") or "")
        if not case_id:
            blockers.append(CODE_MATRIX_CASE_MISSING)
            continue
        if case_id in by_id:
            blockers.append(CODE_MATRIX_CASE_MISSING)
        by_id[case_id] = item
    if set(by_id) != REQUIRED_CASE_IDS:
        blockers.append(CODE_MATRIX_CASE_MISSING)
    for case_id, expected_rewrite in CASE_EXPECT_WOULD_REWRITE.items():
        row = by_id.get(case_id)
        if not isinstance(row, Mapping):
            continue
        if row.get("ok") is not True:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        if row.get("would_rewrite") is not expected_rewrite:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        if row.get("byte_identical") is not True:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        expected_action = CASE_EXPECT_GUARD_ACTION.get(case_id)
        if expected_action and row.get("guard_action") != expected_action:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        if row.get("replaced") is True:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
        if row.get("customer_text_changed") is True:
            blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    guards = matrix.get("guards")
    if not isinstance(guards, Mapping):
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    else:
        for key in (
            "customer_text_changed_count",
            "additional_llm_calls",
            "duplicate_invocation_count",
            "outbound_provider_calls",
        ):
            if _safe_counter_int(guards.get(key)) != 0:
                blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    return blockers


def validate_stable_counters(counters: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not isinstance(counters, Mapping):
        return [CODE_STABLE_COUNTERS_DRIFT]
    for key, expected_value in EXPECTED_STABLE_COUNTERS.items():
        if key not in counters:
            blockers.append(CODE_STABLE_COUNTERS_DRIFT)
            continue
        parsed = _safe_counter_int(counters.get(key))
        if parsed is None or parsed != expected_value:
            blockers.append(CODE_STABLE_COUNTERS_DRIFT)
    return blockers


def _safe_non_negative_int(value: Any) -> int | None:
    parsed = _safe_counter_int(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _restart_snapshot_identity_matches(
    snapshot_binding: Mapping[str, Any],
    expected: Mapping[str, str],
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(snapshot_binding, Mapping):
        return [CODE_RESTART_EVIDENCE_INVALID]
    for key in RESTART_IDENTITY_BINDING_KEYS:
        if str(snapshot_binding.get(key) or "") != str(expected.get(key) or ""):
            blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    return blockers


def validate_restart_evidence_snapshot(
    snapshot: Any,
    *,
    expected_identity: Mapping[str, str],
    require_production: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(snapshot, Mapping):
        return [CODE_RESTART_EVIDENCE_INVALID]
    missing = RESTART_EVIDENCE_SNAPSHOT_KEYS - set(snapshot)
    if missing:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    if _parse_utc(snapshot.get("collected_at_utc")) is None:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    starttime = _safe_non_negative_int(snapshot.get("pid1_starttime_ticks"))
    if starttime is None:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    cmdline = str(snapshot.get("pid1_cmdline") or "").strip()
    if not cmdline:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    identity = snapshot.get("identity_binding")
    if isinstance(identity, Mapping):
        blockers.extend(
            validate_identity_binding_shape(identity, require_production=require_production)
        )
        blockers.extend(_restart_snapshot_identity_matches(identity, expected_identity))
    else:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    return blockers


def validate_restart_evidence(
    restart: Any,
    *,
    expected_identity: Mapping[str, str] | None = None,
    require_production: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(restart, Mapping):
        return [CODE_RESTART_EVIDENCE_INVALID]
    proof_mode = str(restart.get("proof_mode") or "").strip()
    if not proof_mode:
        if str(restart.get("prior_container_id") or "").strip() and str(restart.get("new_container_id") or "").strip():
            proof_mode = RESTART_PROOF_CONTAINER_ID_CHANGE
        elif isinstance(restart.get("pre_restart"), Mapping) and isinstance(restart.get("post_restart"), Mapping):
            proof_mode = RESTART_PROOF_PID1_STARTTIME_CHANGE
    if proof_mode not in {RESTART_PROOF_CONTAINER_ID_CHANGE, RESTART_PROOF_PID1_STARTTIME_CHANGE}:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
        return blockers
    restart_completed = _parse_utc(restart.get("restart_completed_at_utc"))
    if restart_completed is None:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)

    if proof_mode == RESTART_PROOF_CONTAINER_ID_CHANGE:
        for key in ("prior_container_id", "new_container_id"):
            if not str(restart.get(key) or "").strip():
                blockers.append(CODE_RESTART_EVIDENCE_INVALID)
        prior_cid = str(restart.get("prior_container_id") or "")
        new_cid = str(restart.get("new_container_id") or "")
        if prior_cid and new_cid and prior_cid == new_cid:
            blockers.append(CODE_RESTART_EVIDENCE_INVALID)
        return blockers

    if str(restart.get("collection_method") or "") != RESTART_COLLECTION_METHOD_PROC1_STAT_FIELD22:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    if expected_identity is None:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
        return blockers

    pre = restart.get("pre_restart")
    post = restart.get("post_restart")
    blockers.extend(
        validate_restart_evidence_snapshot(
            pre,
            expected_identity=expected_identity,
            require_production=require_production,
        )
    )
    blockers.extend(
        validate_restart_evidence_snapshot(
            post,
            expected_identity=expected_identity,
            require_production=require_production,
        )
    )
    if blockers:
        return blockers

    if not isinstance(pre, Mapping) or not isinstance(post, Mapping):
        return blockers + [CODE_RESTART_EVIDENCE_INVALID]

    pre_ticks = _safe_non_negative_int(pre.get("pid1_starttime_ticks"))
    post_ticks = _safe_non_negative_int(post.get("pid1_starttime_ticks"))
    if pre_ticks is None or post_ticks is None:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    elif pre_ticks == post_ticks:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    elif post_ticks < pre_ticks:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)

    pre_cmd = str(pre.get("pid1_cmdline") or "").strip()
    post_cmd = str(post.get("pid1_cmdline") or "").strip()
    if not pre_cmd or pre_cmd != post_cmd:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)

    pre_ts = _parse_utc(pre.get("collected_at_utc"))
    post_ts = _parse_utc(post.get("collected_at_utc"))
    if pre_ts is None or post_ts is None or post_ts <= pre_ts:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    if restart_completed and pre_ts and restart_completed < pre_ts:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    if restart_completed and post_ts and restart_completed < post_ts:
        blockers.append(CODE_RESTART_EVIDENCE_INVALID)

    # Reject caller-supplied self-assertion without in-container collection proof.
    for key in (
        "restart_confirmed",
        "restart_asserted",
        "hostname_changed",
        "pid1_restarted",
        "proof_asserted",
    ):
        if key in restart:
            blockers.append(CODE_RESTART_EVIDENCE_INVALID)
    for snapshot in (pre, post):
        for key in ("restart_confirmed", "restart_asserted", "proof_asserted"):
            if key in snapshot:
                blockers.append(CODE_RESTART_EVIDENCE_INVALID)

    return blockers


def validate_isolated_service_constraints(constraints: Any) -> list[str]:
    if not isinstance(constraints, Mapping):
        return [CODE_PHASE_ARTIFACT_INVALID]
    blockers: list[str] = []
    if constraints.get("no_domains") is not True:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    if constraints.get("no_provider_credentials") is not True:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    return blockers


def validate_lifecycle_attestation(
    *,
    phase: str,
    attestation: Any,
    baseline_deployment_id: str | None = None,
    redeploy_deployment_id: str | None = None,
    expected_identity: Mapping[str, str] | None = None,
    require_production: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if not isinstance(attestation, Mapping):
        return [CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID]
    if str(attestation.get("phase") or "") != phase:
        blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
    action = str(attestation.get("action") or "")
    if phase == PHASE_BASELINE:
        if action != "initial_deploy":
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
    elif phase == PHASE_CONTAINER_RESTART:
        if action != "container_restart":
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
        restart = attestation.get("restart_evidence")
        restart_blockers = validate_restart_evidence(
            restart,
            expected_identity=expected_identity,
            require_production=require_production,
        )
        if restart_blockers:
            blockers.extend(restart_blockers)
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
    elif phase == PHASE_FRESH_PINNED_REDEPLOY:
        if action != "fresh_pinned_redeploy":
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
        prior = str(attestation.get("prior_deployment_id") or "")
        new = str(attestation.get("new_deployment_id") or "")
        if not _UUID_RE.fullmatch(prior) or not _UUID_RE.fullmatch(new):
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
        if baseline_deployment_id and prior != baseline_deployment_id:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
        if prior == new:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
        if redeploy_deployment_id and new != redeploy_deployment_id:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
    elif phase in {PHASE_REPEAT_MATRIX_1, PHASE_REPEAT_MATRIX_2, PHASE_REPEAT_MATRIX_3}:
        if action != "repeat_matrix":
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
        seq = _safe_positive_int(attestation.get("sequence"))
        expected = int(phase.rsplit("_", 1)[-1])
        if seq is None or seq != expected:
            blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
        if redeploy_deployment_id and str(attestation.get("deployment_id") or "") != redeploy_deployment_id:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
    else:
        blockers.append(CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID)
    return blockers


def validate_phase_artifact(
    artifact: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, str] | None = None,
    baseline_deployment_id: str | None = None,
    redeploy_deployment_id: str | None = None,
    baseline_image_digest: str | None = None,
    redeploy_image_digest: str | None = None,
    require_production_provenance: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if require_production_provenance and artifact.get("fixture_synthetic") is True:
        blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
    if artifact.get("phase_artifact_schema_version") != PHASE_ARTIFACT_SCHEMA_VERSION:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    phase = str(artifact.get("phase") or "")
    if phase not in LIFECYCLE_PHASES:
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
    if artifact.get("execution_mode") != EXECUTION_MODE_IN_CONTAINER:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    if str(artifact.get("target_app_root") or "") != DEPLOYMENT_APP_ROOT:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    if _parse_utc(artifact.get("executed_at_utc")) is None:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    identity = artifact.get("identity_binding")
    if isinstance(identity, Mapping):
        blockers.extend(
            validate_identity_binding_shape(identity, require_production=require_production_provenance)
        )
        if expected_identity:
            phase_expected = {
                key: value
                for key, value in expected_identity.items()
                if key not in {"deployment_id", "image_digest"}
            }
            blockers.extend(
                identity_binding_matches_expected(
                    identity,
                    phase_expected,
                    require_production=require_production_provenance,
                )
            )
        if identity.get("service_role") != SERVICE_ROLE_ISOLATED_PREPROD_SHADOW:
            blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
        deployment_id = str(identity.get("deployment_id") or "")
        image_digest = str(identity.get("image_digest") or "").strip().lower()
        if require_production_provenance and not validate_production_image_digest(image_digest):
            blockers.append(CODE_IMAGE_DIGEST_INVALID)
        if phase in {PHASE_BASELINE, PHASE_CONTAINER_RESTART}:
            if baseline_deployment_id and deployment_id != baseline_deployment_id:
                blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
            if baseline_image_digest and image_digest != baseline_image_digest:
                blockers.append(CODE_IMAGE_DIGEST_INCONSISTENT)
        elif phase == PHASE_FRESH_PINNED_REDEPLOY:
            attestation = artifact.get("lifecycle_attestation")
            new_id = str(attestation.get("new_deployment_id") or "") if isinstance(attestation, Mapping) else ""
            if redeploy_deployment_id and deployment_id != redeploy_deployment_id:
                blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
            if new_id and deployment_id != new_id:
                blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
            if redeploy_image_digest and image_digest != redeploy_image_digest:
                blockers.append(CODE_IMAGE_DIGEST_INCONSISTENT)
        elif phase in {PHASE_REPEAT_MATRIX_1, PHASE_REPEAT_MATRIX_2, PHASE_REPEAT_MATRIX_3}:
            if redeploy_deployment_id and deployment_id != redeploy_deployment_id:
                blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
            if redeploy_image_digest and image_digest != redeploy_image_digest:
                blockers.append(CODE_IMAGE_DIGEST_INCONSISTENT)
    else:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    matrix = artifact.get("matrix")
    if isinstance(matrix, Mapping):
        blockers.extend(validate_matrix_payload(matrix))
        extracted, extract_blockers = safe_extract_stable_counters(matrix)
        blockers.extend(extract_blockers)
        if extracted is not None:
            blockers.extend(validate_stable_counters(extracted))
        blockers.extend(
            validate_artifact_stable_counters_consistency(
                matrix=matrix,
                counters=artifact.get("stable_counters"),
            )
        )
    else:
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    blockers.extend(
        validate_isolated_service_constraints(artifact.get("isolated_service_constraints"))
    )
    phase_identity = identity if isinstance(identity, Mapping) else None
    blockers.extend(
        validate_lifecycle_attestation(
            phase=phase,
            attestation=artifact.get("lifecycle_attestation"),
            baseline_deployment_id=baseline_deployment_id,
            redeploy_deployment_id=redeploy_deployment_id,
            expected_identity=phase_identity if phase == PHASE_CONTAINER_RESTART else None,
            require_production=require_production_provenance,
        )
    )
    dependency_fault = artifact.get("dependency_fault")
    if dependency_fault is not None:
        if not isinstance(dependency_fault, Mapping):
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif dependency_fault.get("status") != DEPENDENCY_FAULT_SKIPPED:
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif not str(dependency_fault.get("residual_risk") or "").strip():
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
    return blockers


def validate_phase_timestamp_order(phases: list[Mapping[str, Any]]) -> list[str]:
    blockers: list[str] = []
    timestamps: list[datetime] = []
    by_phase: dict[str, Mapping[str, Any]] = {}
    for row in phases:
        phase = str(row.get("phase") or "")
        by_phase[phase] = row
        ts = _parse_utc(row.get("executed_at_utc"))
        if ts is None:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
            continue
        timestamps.append(ts)
    for left, right in zip(timestamps, timestamps[1:]):
        if right <= left:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)

    baseline = by_phase.get(PHASE_BASELINE)
    restart = by_phase.get(PHASE_CONTAINER_RESTART)
    if isinstance(baseline, Mapping) and isinstance(restart, Mapping):
        baseline_ts = _parse_utc(baseline.get("executed_at_utc"))
        restart_ts = _parse_utc(restart.get("executed_at_utc"))
        attestation = restart.get("lifecycle_attestation")
        restart_completed = None
        if isinstance(attestation, Mapping):
            restart_evidence = attestation.get("restart_evidence")
            if isinstance(restart_evidence, Mapping):
                restart_completed = _parse_utc(restart_evidence.get("restart_completed_at_utc"))
                proof_mode = str(restart_evidence.get("proof_mode") or "")
                if proof_mode == RESTART_PROOF_PID1_STARTTIME_CHANGE:
                    pre = restart_evidence.get("pre_restart")
                    post = restart_evidence.get("post_restart")
                    if isinstance(pre, Mapping) and isinstance(post, Mapping):
                        pre_collected = _parse_utc(pre.get("collected_at_utc"))
                        post_collected = _parse_utc(post.get("collected_at_utc"))
                        if baseline_ts and pre_collected and pre_collected <= baseline_ts:
                            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
                        if pre_collected and post_collected and post_collected <= pre_collected:
                            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
                        if restart_completed and post_collected and restart_completed < post_collected:
                            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
        if baseline_ts and restart_completed and restart_completed <= baseline_ts:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
        if restart_completed and restart_ts and restart_completed > restart_ts:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)

    fresh = by_phase.get(PHASE_FRESH_PINNED_REDEPLOY)
    repeat_rows = [
        by_phase[phase]
        for phase in (PHASE_REPEAT_MATRIX_1, PHASE_REPEAT_MATRIX_2, PHASE_REPEAT_MATRIX_3)
        if phase in by_phase
    ]
    if isinstance(fresh, Mapping) and repeat_rows:
        fresh_ts = _parse_utc(fresh.get("executed_at_utc"))
        first_repeat_ts = _parse_utc(repeat_rows[0].get("executed_at_utc"))
        if fresh_ts and first_repeat_ts:
            if (first_repeat_ts - fresh_ts).total_seconds() < REPEAT_MATRIX_MIN_SPACING_SECONDS:
                blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
    for left, right in zip(repeat_rows, repeat_rows[1:]):
        lts = _parse_utc(left.get("executed_at_utc"))
        rts = _parse_utc(right.get("executed_at_utc"))
        if lts is None or rts is None:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
            continue
        if (rts - lts).total_seconds() < REPEAT_MATRIX_MIN_SPACING_SECONDS:
            blockers.append(CODE_PHASE_TIMESTAMP_ORDER_INVALID)
    return blockers


def validate_lifecycle_phase_identities(phases: list[Mapping[str, Any]]) -> list[str]:
    blockers: list[str] = []
    by_phase: dict[str, Mapping[str, Any]] = {}
    for row in phases:
        if isinstance(row, Mapping):
            by_phase[str(row.get("phase") or "")] = row
    if set(by_phase) != set(LIFECYCLE_PHASES):
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
        return blockers

    baseline_row = by_phase[PHASE_BASELINE]
    baseline_identity = baseline_row.get("identity_binding")
    if not isinstance(baseline_identity, Mapping):
        return blockers + [CODE_IDENTITY_BINDING_MISMATCH]
    baseline_deployment = str(baseline_identity.get("deployment_id") or "")
    baseline_image = str(baseline_identity.get("image_digest") or "").strip().lower()
    shared_keys = ("pinned_target_revision", "manifest_digest", "service_role", "service_name", "service_id")

    for phase in LIFECYCLE_PHASES:
        row = by_phase[phase]
        identity = row.get("identity_binding")
        if not isinstance(identity, Mapping):
            blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
            continue
        for key in shared_keys:
            if str(identity.get(key) or "") != str(baseline_identity.get(key) or ""):
                blockers.append(CODE_PHASE_IDENTITY_INCONSISTENT)

    restart_identity = by_phase[PHASE_CONTAINER_RESTART].get("identity_binding")
    if isinstance(restart_identity, Mapping):
        if str(restart_identity.get("deployment_id") or "") != baseline_deployment:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
        if str(restart_identity.get("image_digest") or "").strip().lower() != baseline_image:
            blockers.append(CODE_IMAGE_DIGEST_INCONSISTENT)

    fresh_row = by_phase[PHASE_FRESH_PINNED_REDEPLOY]
    fresh_identity = fresh_row.get("identity_binding")
    fresh_attestation = fresh_row.get("lifecycle_attestation")
    redeploy_deployment = ""
    redeploy_image = ""
    if isinstance(fresh_identity, Mapping):
        redeploy_deployment = str(fresh_identity.get("deployment_id") or "")
        redeploy_image = str(fresh_identity.get("image_digest") or "").strip().lower()
    if isinstance(fresh_attestation, Mapping):
        new_id = str(fresh_attestation.get("new_deployment_id") or "")
        if redeploy_deployment and new_id and redeploy_deployment != new_id:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
        if baseline_deployment and new_id and new_id == baseline_deployment:
            blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)

    for phase in (PHASE_REPEAT_MATRIX_1, PHASE_REPEAT_MATRIX_2, PHASE_REPEAT_MATRIX_3):
        identity = by_phase[phase].get("identity_binding")
        if isinstance(identity, Mapping):
            if redeploy_deployment and str(identity.get("deployment_id") or "") != redeploy_deployment:
                blockers.append(CODE_PHASE_DEPLOYMENT_ID_INVALID)
            if redeploy_image and str(identity.get("image_digest") or "").strip().lower() != redeploy_image:
                blockers.append(CODE_IMAGE_DIGEST_INCONSISTENT)
    return blockers


def validate_bundle_timestamps(
    *,
    phases: list[Mapping[str, Any]],
    signed_at_utc: Any,
    teardown_proof: Mapping[str, Any] | None,
    negative_controls: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> list[str]:
    blockers: list[str] = []
    reference = now or datetime.now(timezone.utc)
    signed_at = _parse_utc(signed_at_utc)
    if signed_at is None:
        blockers.append(CODE_TIMESTAMP_INVALID)
        return blockers
    if signed_at > reference + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
        blockers.append(CODE_TIMESTAMP_INVALID)

    phase_times: list[datetime] = []
    for row in phases:
        ts = _parse_utc(row.get("executed_at_utc")) if isinstance(row, Mapping) else None
        if ts is None:
            blockers.append(CODE_TIMESTAMP_INVALID)
            continue
        if ts > reference + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
            blockers.append(CODE_TIMESTAMP_INVALID)
        phase_times.append(ts)
    if not phase_times:
        return blockers
    latest_phase = max(phase_times)
    if signed_at < latest_phase:
        blockers.append(CODE_TIMESTAMP_INVALID)
    if (reference - latest_phase).total_seconds() > ARTIFACT_MAX_AGE_SECONDS:
        blockers.append(CODE_TIMESTAMP_STALE)

    if isinstance(teardown_proof, Mapping):
        isolated = teardown_proof.get("isolated_service")
        canonical = teardown_proof.get("canonical_control")
        for section in (isolated, canonical):
            if isinstance(section, Mapping):
                verified = _parse_utc(section.get("verified_at_utc"))
                if verified is None:
                    blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
                elif verified < latest_phase or verified > signed_at:
                    blockers.append(CODE_TIMESTAMP_INVALID)
                elif verified > reference + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
                    blockers.append(CODE_TIMESTAMP_INVALID)

    if isinstance(negative_controls, Mapping):
        controls = negative_controls.get("controls")
        if isinstance(controls, list):
            for row in controls:
                if not isinstance(row, Mapping):
                    blockers.append(CODE_TIMESTAMP_INVALID)
                    continue
                executed = _parse_utc(row.get("executed_at_utc"))
                if executed is None:
                    blockers.append(CODE_TIMESTAMP_INVALID)
                    continue
                if executed > reference + timedelta(seconds=CLOCK_SKEW_ALLOWANCE_SECONDS):
                    blockers.append(CODE_TIMESTAMP_INVALID)
                if executed < latest_phase or executed > signed_at:
                    blockers.append(CODE_TIMESTAMP_INVALID)
    return blockers


def validate_negative_control_artifact(
    artifact: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, str],
    require_production_provenance: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if require_production_provenance and artifact.get("fixture_synthetic") is True:
        blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
    if artifact.get("negative_control_schema_version") != NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION:
        blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
    control_id = str(artifact.get("control_id") or "")
    if control_id not in NEGATIVE_CONTROL_IDS:
        blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
    if _parse_utc(artifact.get("executed_at_utc")) is None:
        blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
    if artifact.get("execution_mode") != EXECUTION_MODE_IN_CONTAINER:
        blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
    if str(artifact.get("target_app_root") or "") != DEPLOYMENT_APP_ROOT:
        blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
    identity = artifact.get("identity_binding")
    if isinstance(identity, Mapping):
        blockers.extend(
            identity_binding_matches_expected(
                identity,
                expected_identity,
                require_production=require_production_provenance,
            )
        )
    else:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    expected_code = NEGATIVE_CONTROL_EXPECTED_CODES.get(control_id)
    if artifact.get("blocked") is not True or artifact.get("code") != expected_code:
        blockers.append(CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS)
    return blockers


def validate_teardown_proof(
    proof: Mapping[str, Any],
    *,
    expected_isolated: Mapping[str, str] | None = None,
    expected_canonical: Mapping[str, str] | None = None,
    require_production_provenance: bool = False,
) -> list[str]:
    blockers: list[str] = []
    if require_production_provenance and proof.get("fixture_synthetic") is True:
        blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
    if proof.get("teardown_proof_schema_version") != TEARDOWN_PROOF_SCHEMA_VERSION:
        blockers.append(CODE_TEARDOWN_PROOF_MISSING)
    isolated = proof.get("isolated_service")
    canonical = proof.get("canonical_control")
    if not isinstance(isolated, Mapping) or not isinstance(canonical, Mapping):
        blockers.append(CODE_TEARDOWN_PROOF_MISSING)
        return blockers
    if isolated.get("guard_mode") != "off":
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if canonical.get("guard_mode") != "off":
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if isolated.get("service_state") not in ALLOWED_ISOLATED_SERVICE_STATES:
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if _parse_utc(isolated.get("verified_at_utc")) is None:
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if _parse_utc(canonical.get("verified_at_utc")) is None:
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if isolated.get("service_role") != SERVICE_ROLE_ISOLATED_PREPROD_SHADOW:
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if canonical.get("service_role") != SERVICE_ROLE_CANONICAL_CONTROL:
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not str(isolated.get("service_name") or "").strip():
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not str(canonical.get("service_name") or "").strip():
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not _UUID_RE.fullmatch(str(isolated.get("service_id") or "").strip()):
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not _UUID_RE.fullmatch(str(canonical.get("service_id") or "").strip()):
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not _UUID_RE.fullmatch(str(isolated.get("deployment_id") or "").strip()):
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if not _UUID_RE.fullmatch(str(canonical.get("deployment_id") or "").strip()):
        blockers.append(CODE_TEARDOWN_PROOF_UNVERIFIED)
    if expected_isolated:
        for key in ("service_role", "service_name", "service_id", "deployment_id"):
            if str(isolated.get(key) or "") != str(expected_isolated.get(key) or ""):
                blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    if expected_canonical:
        for key in ("service_role", "service_name", "service_id", "deployment_id"):
            if str(canonical.get(key) or "") != str(expected_canonical.get(key) or ""):
                blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    return blockers


def validate_superseded_windows(
    rows: Any,
    *,
    require_migration_windows: bool = False,
    require_production_provenance: bool = False,
) -> list[str]:
    if not isinstance(rows, list) or not rows:
        return [CODE_SUPERSEDED_WINDOWS_MISSING]
    blockers: list[str] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            blockers.append(CODE_SUPERSEDED_WINDOWS_MISSING)
            continue
        if require_production_provenance and row.get("fixture_synthetic") is True:
            blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
        window_id = str(row.get("window_id") or "").strip()
        if not window_id:
            blockers.append(CODE_SUPERSEDED_WINDOWS_MISSING)
        else:
            seen_ids.add(window_id)
        if not str(row.get("reason") or "").strip():
            blockers.append(CODE_SUPERSEDED_WINDOWS_MISSING)
        if _parse_utc(row.get("superseded_at_utc")) is None:
            blockers.append(CODE_SUPERSEDED_WINDOWS_MISSING)
        if row.get("active") is True:
            blockers.append(CODE_SUPERSEDED_WINDOW_ACTIVE)
    if require_migration_windows and not REQUIRED_SUPERSEDED_WINDOW_IDS <= seen_ids:
        blockers.append(CODE_SUPERSEDED_WINDOWS_MISSING)
    return blockers


def validate_lifecycle_phase_row(row: Mapping[str, Any]) -> list[str]:
    """Accumulate all lifecycle phase blockers without early return."""
    blockers: list[str] = []
    phase = str(row.get("phase") or "")
    if phase not in LIFECYCLE_PHASES:
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
    if row.get("ok") is not True:
        blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
    matrix = row.get("matrix")
    if not isinstance(matrix, Mapping):
        blockers.append(CODE_MATRIX_INVARIANT_VIOLATION)
    else:
        blockers.extend(validate_matrix_payload(matrix))
        extracted, extract_blockers = safe_extract_stable_counters(matrix)
        blockers.extend(extract_blockers)
        if extracted is not None:
            blockers.extend(validate_stable_counters(extracted))
        blockers.extend(
            validate_artifact_stable_counters_consistency(
                matrix=matrix,
                counters=row.get("stable_counters"),
            )
        )
    if row.get("execution_mode") != EXECUTION_MODE_IN_CONTAINER:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    if str(row.get("target_app_root") or "") != DEPLOYMENT_APP_ROOT:
        blockers.append(CODE_PHASE_ARTIFACT_INVALID)
    identity = row.get("identity_binding")
    if isinstance(identity, Mapping):
        blockers.extend(validate_identity_binding_shape(identity))
    else:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    blockers.extend(
        validate_isolated_service_constraints(row.get("isolated_service_constraints"))
    )
    dependency_fault = row.get("dependency_fault")
    if dependency_fault is not None:
        if not isinstance(dependency_fault, Mapping):
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif dependency_fault.get("status") != DEPENDENCY_FAULT_SKIPPED:
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        elif not str(dependency_fault.get("residual_risk") or "").strip():
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
    return blockers


__all__ = [
    "ALLOWED_ISOLATED_SERVICE_STATES",
    "BUNDLE_SCHEMA_VERSION",
    "BASELINE_IMAGE_DIGEST_ENV",
    "CANONICAL_DEPLOYMENT_ID_ENV",
    "CANONICAL_SERVICE_ID_ENV",
    "CANONICAL_SERVICE_NAME_ENV",
    "CASE_EXPECT_GUARD_ACTION",
    "CASE_EXPECT_WOULD_REWRITE",
    "CLOCK_SKEW_ALLOWANCE_SECONDS",
    "ARTIFACT_MAX_AGE_SECONDS",
    "CODE_ARCH001_SIGNOFF_MISSING",
    "CODE_ARTIFACT_UNREADABLE",
    "CODE_BUNDLE_INVALID",
    "CODE_BUNDLE_SIGNATURE_INVALID",
    "CODE_COMMAND_INVALID",
    "CODE_EVIDENCE_CLASS_INELIGIBLE",
    "CODE_EXPECTED_IDENTITY_MISSING",
    "CODE_HMAC_KEY_WEAK",
    "CODE_IDENTITY_BINDING_MISMATCH",
    "CODE_LEGACY_V1_NOT_SUFFICIENT",
    "CODE_LIFECYCLE_PHASE_FAILED",
    "CODE_LIFECYCLE_PHASE_MISSING",
    "CODE_MATRIX_CASE_MISSING",
    "CODE_MATRIX_INVARIANT_VIOLATION",
    "CODE_NEGATIVE_CONTROL_MISSING",
    "CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS",
    "CODE_PHASE_ARTIFACT_INVALID",
    "CODE_PHASE_DEPLOYMENT_ID_INVALID",
    "CODE_PHASE_LIFECYCLE_ATTESTATION_INVALID",
    "CODE_PHASE_TIMESTAMP_ORDER_INVALID",
    "CODE_POST_APPROVAL_NOT_PENDING",
    "CODE_PRODUCTION_SYNTHETIC_MARKER",
    "CODE_RUNTIME_BINDING_MISMATCH",
    "CODE_TIMESTAMP_INVALID",
    "CODE_TIMESTAMP_STALE",
    "CODE_PHASE_IDENTITY_INCONSISTENT",
    "CODE_IMAGE_DIGEST_INCONSISTENT",
    "CODE_IMAGE_DIGEST_INVALID",
    "CODE_RESTART_EVIDENCE_INVALID",
    "CODE_REVISION_FORMAT_INVALID",
    "CODE_STABLE_COUNTERS_DRIFT",
    "CODE_SUPERSEDED_WINDOW_ACTIVE",
    "CODE_SUPERSEDED_WINDOWS_MISSING",
    "CODE_TEARDOWN_PROOF_MISSING",
    "CODE_TEARDOWN_PROOF_UNVERIFIED",
    "CODE_TRAFFIC_CLAIM_INVALID",
    "DEPENDENCY_FAULT_SKIPPED",
    "DEPLOYMENT_APP_ROOT",
    "ELIGIBLE_EVIDENCE_CLASSES",
    "ENFORCE_MODE_VALUE",
    "EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST",
    "EVIDENCE_CLASS_PRODUCTION_SIGNOFF",
    "EXECUTION_MODE_IN_CONTAINER",
    "EXPECTED_IMAGE_DIGEST_ENV",
    "EXPECTED_MANIFEST_DIGEST_ENV",
    "EXPECTED_STABLE_COUNTERS",
    "HMAC_DOMAIN_PREFIX",
    "IDENTITY_BINDING_KEYS",
    "INITIATIVE_ID",
    "ISOLATED_DEPLOYMENT_ID_ENV",
    "ISOLATED_SERVICE_CONSTRAINT_KEYS",
    "ISOLATED_SERVICE_ID_ENV",
    "ISOLATED_SERVICE_NAME_ENV",
    "KNOWN_REJECTED_HMAC_KEYS",
    "LEGACY_V1_SCHEMA_VERSION",
    "LIFECYCLE_PHASES",
    "MATRIX_REPORT_SCHEMA_VERSION",
    "MIN_HMAC_KEY_BYTES",
    "NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION",
    "NEGATIVE_CONTROL_EXPECTED_CODES",
    "NEGATIVE_CONTROL_IDS",
    "PHASE_ARTIFACT_SCHEMA_VERSION",
    "PHASE_BASELINE",
    "PHASE_BUNDLE",
    "PHASE_CONTAINER_RESTART",
    "PHASE_CONTRACT_SELF_TEST",
    "PHASE_FRESH_PINNED_REDEPLOY",
    "PHASE_LEGACY_V1_READ",
    "PHASE_NEGATIVE_CONTROLS",
    "PHASE_REPEAT_MATRIX_1",
    "PHASE_REPEAT_MATRIX_2",
    "PHASE_REPEAT_MATRIX_3",
    "PHASE_VERIFY",
    "PINNED_REVISION_ENV",
    "POST_APPROVAL_PENDING",
    "REQUIRED_SUPERSEDED_WINDOW_IDS",
    "REPEAT_MATRIX_MIN_SPACING_SECONDS",
    "RESTART_IDENTITY_BINDING_KEYS",
    "REQUIRED_CASE_IDS",
    "SERVICE_ROLE_CANONICAL_CONTROL",
    "SERVICE_ROLE_ISOLATED_PREPROD_SHADOW",
    "SHADOW_MODE_ENV",
    "SHADOW_MODE_VALUE",
    "SIGNOFF_ARTIFACT_ENV",
    "SIGNOFF_HMAC_KEY_ENV",
    "TEARDOWN_PROOF_SCHEMA_VERSION",
    "TRAFFIC_CLAIM",
    "extract_stable_counters",
    "identity_binding_matches_expected",
    "is_legacy_v1_bundle",
    "is_strong_hmac_key",
    "safe_extract_stable_counters",
    "validate_artifact_stable_counters_consistency",
    "validate_identity_binding_shape",
    "validate_bundle_timestamps",
    "validate_image_digest_value",
    "validate_lifecycle_attestation",
    "validate_lifecycle_phase_identities",
    "validate_lifecycle_phase_row",
    "validate_matrix_payload",
    "validate_negative_control_artifact",
    "validate_phase_artifact",
    "validate_phase_timestamp_order",
    "validate_production_image_digest",
    "validate_restart_evidence",
    "validate_restart_evidence_snapshot",
    "validate_revision_token",
    "validate_stable_counters",
    "validate_superseded_windows",
    "validate_teardown_proof",
]
