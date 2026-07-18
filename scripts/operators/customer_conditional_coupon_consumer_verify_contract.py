"""Closed contract for staging conditional-coupon consumer sign-off verification."""
from __future__ import annotations

from typing import Any, Mapping

REPORT_SCHEMA_VERSION = "coupon_consumer_verify_v1"

# Evidence-backed **target runtime** pin from post-#625 consumer revision attestation
# (main @ 8ea344fc). The verifier operator may ship in later tooling commits;
# attestation must prove this exact revision via in-container build injects or an
# external checkout at the pin.
PINNED_TARGET_RUNTIME_REVISION = "8ea344fc9d786c19d9dbaa00ec6d6bc98e683f1e"
PINNED_TARGET_RUNTIME_REVISION_SHORT = PINNED_TARGET_RUNTIME_REVISION[:8]

# Back-compat aliases (PR #624 naming).
PINNED_SOURCE_REVISION = PINNED_TARGET_RUNTIME_REVISION
PINNED_SOURCE_REVISION_SHORT = PINNED_TARGET_RUNTIME_REVISION_SHORT

FIXTURE_TENANT_ID = 1
FIXTURE_CUSTOMER_PHONE = "966500000099"
GENERIC_STORE_NAME = "متجر تجريبي عام"
GENERIC_STORE_URL = "https://example.test"

# Inbound probe message (not outbound conversational prose).
MESSAGE_ELIGIBLE = "conditional coupon after min orders for loyalty offer"

# Deterministic compose/LLM stubs for bounded process-scoped activation probes.
PROBE_PERSONA_COMPOSE_STUB = "__PROBE_PERSONA_COMPOSE_STUB__"
PROBE_SAFE_GENERAL_LLM_STUB = "__PROBE_SAFE_GENERAL_LLM_STUB__"
PROBE_UNSAFE_GENERAL_LLM_STUB = "coupon code SAVE20 issued your coupon now"
PROBE_DEDUP_BEFORE_STUB = "__PROBE_DEDUP_BEFORE_STUB__"
PROBE_DEDUP_AFTER_STUB = "__PROBE_DEDUP_AFTER_STUB__"
PROBE_DEDUP_SNAPSHOT_ID = "snap-consumer-verify-dedup-probe"

MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN = 1
MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN = 1

PHASE_ARTIFACT_PREFLIGHT = "artifact_preflight"
PHASE_RUNTIME_REVISION_ATTESTATION = "runtime_revision_attestation"
PHASE_DEFAULT_OFF = "default_off"
PHASE_A1_CAPABILITY = "a1_capability"
PHASE_SHADOW_OBSERVATION = "shadow_observation"
PHASE_PROJECTION_INELIGIBLE = "projection_ineligible"
PHASE_COMPOSE_CANARY_DENIED = "compose_canary_denied"
PHASE_COMPOSE_CANARY_ALLOWED_PREFLIGHT = "compose_canary_allowed_preflight"
PHASE_COMPOSE_PERSONA_SUCCESS = "compose_persona_success"
PHASE_COMPOSE_GENERAL_LLM_SAFE = "compose_general_llm_safe"
PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD = "compose_general_llm_unsafe_guard"
PHASE_WEBHOOK_DEDUP = "webhook_dedup"
PHASE_TEARDOWN_FLAGS = "teardown_flags"
PHASE_SUMMARY = "summary"

GATE_PHASES = frozenset(
    {
        PHASE_ARTIFACT_PREFLIGHT,
        PHASE_RUNTIME_REVISION_ATTESTATION,
        PHASE_DEFAULT_OFF,
        PHASE_A1_CAPABILITY,
        PHASE_SHADOW_OBSERVATION,
        PHASE_PROJECTION_INELIGIBLE,
        PHASE_COMPOSE_CANARY_DENIED,
        PHASE_COMPOSE_CANARY_ALLOWED_PREFLIGHT,
        PHASE_COMPOSE_PERSONA_SUCCESS,
        PHASE_COMPOSE_GENERAL_LLM_SAFE,
        PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD,
        PHASE_WEBHOOK_DEDUP,
        PHASE_TEARDOWN_FLAGS,
    }
)

REQUIRED_SUMMARY_KEYS = frozenset({"ok", "phase", "report_schema_version", "results"})

CODE_COMMAND_INVALID = "command_invalid"
CODE_PROBE_FAILED = "probe_failed"
CODE_PINNED_REVISION_MISMATCH = "pinned_revision_mismatch"
CODE_RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"
CODE_RUNTIME_REVISION_UNKNOWN = "runtime_revision_unknown"
CODE_TARGET_APP_ROOT_REQUIRED = "target_app_root_required"
CODE_TEARDOWN_FLAGS_STILL_SET = "teardown_flags_still_set"
CODE_OUTBOUND_PROVIDER_CALLED = "outbound_provider_called"
CODE_DB_GATE_SKIPPED = "db_gate_skipped"

SHADOW_FLAG_ENV = "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED"
COMPOSE_FLAG_ENV = "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED"

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


def eligible_compose_canary_ai_settings(
    *,
    tenant_id: int = FIXTURE_TENANT_ID,
    phone: str = FIXTURE_CUSTOMER_PHONE,
) -> dict[str, Any]:
    """Closed test-mode canary settings for staging consumer probes."""
    return {
        "store_ai_mode": "test",
        "customer_conditional_coupon_compose_allowlist_tenants": [int(tenant_id)],
        "ai_test_allowed_numbers": [str(phone)],
    }


def normalize_pinned_revision(raw: str | None) -> str:
    value = str(raw or PINNED_TARGET_RUNTIME_REVISION).strip().lower()
    if not value:
        raise ValueError(CODE_PINNED_REVISION_MISMATCH)
    if value == PINNED_TARGET_RUNTIME_REVISION or value == PINNED_TARGET_RUNTIME_REVISION_SHORT:
        return PINNED_TARGET_RUNTIME_REVISION
    raise ValueError(CODE_PINNED_REVISION_MISMATCH)


def env_flag_enabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY_ENV


def validate_gate_report(report: Mapping[str, Any]) -> None:
    phase = report.get("phase")
    if phase not in GATE_PHASES:
        raise ValueError("gate_phase_invalid")
    if report.get("report_schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("report_schema_version_mismatch")


def validate_summary_report(report: Mapping[str, Any]) -> None:
    if set(report) < REQUIRED_SUMMARY_KEYS:
        raise ValueError("summary_shape_invalid")
    if report.get("phase") != PHASE_SUMMARY:
        raise ValueError("summary_phase_invalid")
    if report.get("report_schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("report_schema_version_mismatch")
    results = report.get("results")
    if not isinstance(results, Mapping):
        raise ValueError("summary_results_invalid")


__all__ = [
    "CODE_COMMAND_INVALID",
    "CODE_DB_GATE_SKIPPED",
    "CODE_OUTBOUND_PROVIDER_CALLED",
    "CODE_PINNED_REVISION_MISMATCH",
    "CODE_PROBE_FAILED",
    "CODE_TEARDOWN_FLAGS_STILL_SET",
    "COMPOSE_FLAG_ENV",
    "eligible_compose_canary_ai_settings",
    "FIXTURE_CUSTOMER_PHONE",
    "FIXTURE_TENANT_ID",
    "GATE_PHASES",
    "GENERIC_STORE_NAME",
    "GENERIC_STORE_URL",
    "MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN",
    "MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN",
    "MESSAGE_ELIGIBLE",
    "PHASE_A1_CAPABILITY",
    "PHASE_ARTIFACT_PREFLIGHT",
    "PHASE_COMPOSE_GENERAL_LLM_SAFE",
    "PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD",
    "PHASE_COMPOSE_CANARY_ALLOWED_PREFLIGHT",
    "PHASE_COMPOSE_CANARY_DENIED",
    "PHASE_COMPOSE_PERSONA_SUCCESS",
    "PHASE_DEFAULT_OFF",
    "PHASE_PROJECTION_INELIGIBLE",
    "PHASE_SHADOW_OBSERVATION",
    "PHASE_SUMMARY",
    "PHASE_TEARDOWN_FLAGS",
    "PHASE_WEBHOOK_DEDUP",
    "CODE_RUNTIME_REVISION_MISMATCH",
    "CODE_RUNTIME_REVISION_UNKNOWN",
    "CODE_TARGET_APP_ROOT_REQUIRED",
    "PINNED_SOURCE_REVISION",
    "PINNED_SOURCE_REVISION_SHORT",
    "PINNED_TARGET_RUNTIME_REVISION",
    "PINNED_TARGET_RUNTIME_REVISION_SHORT",
    "PHASE_RUNTIME_REVISION_ATTESTATION",
    "PROBE_DEDUP_AFTER_STUB",
    "PROBE_DEDUP_BEFORE_STUB",
    "PROBE_DEDUP_SNAPSHOT_ID",
    "PROBE_PERSONA_COMPOSE_STUB",
    "PROBE_SAFE_GENERAL_LLM_STUB",
    "PROBE_UNSAFE_GENERAL_LLM_STUB",
    "REPORT_SCHEMA_VERSION",
    "REQUIRED_SUMMARY_KEYS",
    "SHADOW_FLAG_ENV",
    "env_flag_enabled",
    "normalize_pinned_revision",
    "validate_gate_report",
    "validate_summary_report",
]
