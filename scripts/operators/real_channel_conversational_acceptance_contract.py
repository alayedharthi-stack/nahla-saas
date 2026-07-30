"""Closed contract for post-shadow real-channel conversational acceptance (ARCH-001).

Default-off preparation only. No tenant activation, no outbound channel messages,
and no secrets in repository artifacts. Test identities resolve from operator env
at execution time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

REPORT_SCHEMA_VERSION = "real_channel_conversational_acceptance_v2"
MANIFEST_SCHEMA_VERSION = "real_channel_acceptance_scenario_manifest_v2"
EVIDENCE_SCHEMA_VERSION = "real_channel_acceptance_evidence_v2"
SESSION_SCHEMA_VERSION = "real_channel_acceptance_session_v1"

# ── Staging identity (fail-closed) ───────────────────────────────────────────
STAGING_PROJECT_ENV = "RAILWAY_PROJECT_NAME"
STAGING_ENVIRONMENT_ENV = "RAILWAY_ENVIRONMENT_NAME"
STAGING_PROJECT_VALUE = "desirable-growth"
STAGING_ENVIRONMENT_VALUE = "staging"
STAGING_IDENTITY_CLASS = "railway_staging_desirable_growth"

# ── Master execution gates (default off) ─────────────────────────────────────
MASTER_ENABLE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED"
EXECUTION_CONFIRM_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM"
ARCH001_SHADOW_SIGNOFF_ENV = "NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM"
# v2 preprod gate — env-only signoff (ARCH001_SHADOW_SIGNOFF_ENV) is no longer sufficient.
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (  # noqa: E402
    BASELINE_IMAGE_DIGEST_ENV,
    CANONICAL_DEPLOYMENT_ID_ENV,
    CANONICAL_SERVICE_ID_ENV,
    CANONICAL_SERVICE_NAME_ENV,
    EXPECTED_IMAGE_DIGEST_ENV,
    EXPECTED_MANIFEST_DIGEST_ENV as ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST_ENV,
    ISOLATED_DEPLOYMENT_ID_ENV as ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID_ENV,
    ISOLATED_SERVICE_ID_ENV as ARCH001_PREPROD_ISOLATED_SERVICE_ID_ENV,
    ISOLATED_SERVICE_NAME_ENV as ARCH001_PREPROD_ISOLATED_SERVICE_NAME_ENV,
    PINNED_REVISION_ENV as ARCH001_PREPROD_PINNED_REVISION_ENV,
    SIGNOFF_ARTIFACT_ENV as ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV as ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV,
)
TENANT_1_PASS_CONFIRM_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_CONFIRM"
EVIDENCE_HMAC_KEY_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_EVIDENCE_HMAC_KEY"
SESSION_DIR_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_SESSION_DIR"
TENANT_1_PASS_ARTIFACT_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_ARTIFACT"
REVIEWER_ID_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_REVIEWER_ID"

# ── Secret-backed test identity refs (names only; values never committed) ───
PINNED_REVISION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION"
TENANT_1_PHONE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE"
TENANT_33_PHONE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE"
TENANT_48_PHONE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_48_PHONE"
ALLOWLIST_PHONES_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_ALLOWLIST_PHONES"
TENANT_1_CONVERSATION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_CONVERSATION_ID"
TENANT_33_CONVERSATION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_CONVERSATION_ID"
TENANT_48_CONVERSATION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_48_CONVERSATION_ID"

# ── Channel / provider preflight env (existence checked; values never logged) ─
# Meta Cloud API direct is the only acceptance target path.
META_READINESS_REQUIRED_ENV_NAMES: tuple[str, ...] = (
    "META_APP_SECRET",
    "WHATSAPP_TOKEN",
    "WHATSAPP_VERIFY_TOKEN",
    "BACKEND_URL",
)
D360_LEGACY_OBSERVABILITY_ENV_NAMES: tuple[str, ...] = (
    "D360_API_BASE_URL",
    "D360_PARTNER_HUB_BASE",
    "D360_PARTNER_API_KEY",
    "D360_PARTNER_ID",
)
CHANNEL_PREFLIGHT_ENV_NAMES: tuple[str, ...] = (
    "DATABASE_URL",
    *META_READINESS_REQUIRED_ENV_NAMES,
    *D360_LEGACY_OBSERVABILITY_ENV_NAMES,
    "WHATSAPP_API_URL",
)
from scripts.operators.meta_acceptance_channel_evidence_contract import (  # noqa: E402
    CODE_DB_WA_BINDING_INVALID,
    CODE_DB_WA_BINDING_MISSING,
    CODE_DB_WA_BINDING_MISMATCH,
    CODE_ROLLBACK_SNAPSHOT_INVALID,
    CODE_ROLLBACK_SNAPSHOT_MISSING,
    CODE_ROLLBACK_SNAPSHOT_STALE,
    CODE_WEBHOOK_ATTESTATION_BACKEND_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_DEPLOYMENT_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_FORGED,
    CODE_WEBHOOK_ATTESTATION_INVALID,
    CODE_WEBHOOK_ATTESTATION_MISSING,
    CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED,
    CODE_WEBHOOK_ATTESTATION_STALE,
    CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH,
    CODE_WEBHOOK_OBSERVATION_INVALID,
    CODE_WEBHOOK_OBSERVATION_STALE,
    EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
    WEBHOOK_ATTESTATION_ARTIFACT_ENV,
    WEBHOOK_ATTESTATION_HMAC_KEY_ENV,
    evaluate_meta_config_present,
    evaluate_operator_attested_channel_ready,
)
from scripts.operators.staging_migration_operator_gates import (  # noqa: E402
    validate_database_binding,
    validate_staging_identity,
)
from scripts.operators.staging_acceptance_config_consolidation_contract import (  # noqa: E402
    ACCEPTANCE_CUTOVER_LABEL,
    ACCEPTANCE_CUTOVER_SCOPE,
    ACCEPTANCE_TARGET_PROVIDER_PATH,
    build_acceptance_cutover_guidance,
    CODE_CHANNEL_D360_ONLY_LEGACY_PATH,
    CODE_CHANNEL_READINESS_GAP,
    evaluate_meta_channel_readiness,
    is_d360_only_legacy_path,
    LEGACY_PROVIDER_PATH,
    META_DIRECT_WEBHOOK_ROUTE,
    META_ONBOARDING_EXTERNAL_BLOCKER,
    META_ONBOARDING_TARGET_PATH,
    STAGING_DB_WA_BINDING_PROVIDER_VALUE,
    STAGING_DB_WA_BINDING_REQUIRED_FIELDS,
    TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID,
)

# ── Tenants (platform-wide; no merchant-specific runtime logic) ────────────────
TENANT_1_INTENSIVE = 1
TENANT_33_LIMITED = 33
TENANT_48_SALLA_MINIMAL = 48
ACCEPTANCE_TENANTS = frozenset(
    {TENANT_1_INTENSIVE, TENANT_33_LIMITED, TENANT_48_SALLA_MINIMAL}
)

# ── Rate / cost caps (per operator session) ──────────────────────────────────
MAX_SCENARIOS_PER_SESSION = 60
MAX_INBOUND_MESSAGES_PER_SESSION = 120
MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION = 120
MAX_LLM_CALLS_PER_SESSION = 240
MAX_SESSION_COST_USD = 25.0
DEFAULT_SCENARIO_LATENCY_BUDGET_MS = 30_000
DEFAULT_SCENARIO_MAX_LLM_CALLS = 4
DEFAULT_SCENARIO_MAX_TOOL_CALLS = 3

# ── Phases ───────────────────────────────────────────────────────────────────
PHASE_DEFAULT_OFF = "default_off"
PHASE_READINESS_PREFLIGHT = "readiness_preflight"
PHASE_ARCH001_SHADOW_SIGNOFF_GATE = "arch001_shadow_signoff_gate"
PHASE_RUNTIME_REVISION_ATTESTATION = "runtime_revision_attestation"
PHASE_CONFIG_SNAPSHOT = "config_snapshot"
PHASE_CHANNEL_HEALTH = "channel_health"
PHASE_TENANT_1_INTENSIVE = "tenant_1_intensive"
PHASE_TENANT_33_LIMITED = "tenant_33_limited"
PHASE_TENANT_48_SALLA_MINIMAL = "tenant_48_salla_minimal"
PHASE_DEFECT_BUNDLE = "defect_bundle"
PHASE_TEARDOWN = "teardown"
PHASE_SUMMARY = "summary"

GATE_PHASES = frozenset(
    {
        PHASE_DEFAULT_OFF,
        PHASE_READINESS_PREFLIGHT,
        PHASE_ARCH001_SHADOW_SIGNOFF_GATE,
        PHASE_RUNTIME_REVISION_ATTESTATION,
        PHASE_CONFIG_SNAPSHOT,
        PHASE_CHANNEL_HEALTH,
        PHASE_TENANT_1_INTENSIVE,
        PHASE_TENANT_33_LIMITED,
        PHASE_TENANT_48_SALLA_MINIMAL,
        PHASE_DEFECT_BUNDLE,
        PHASE_TEARDOWN,
    }
)

# ── Evidence channel taxonomy ────────────────────────────────────────────────
# A signed HTTP POST created by an operator is an ingress integration probe,
# not provider delivery. Only an event paired with real-device attestation can
# be classified as ACTUAL_PROVIDER_CHANNEL.
EVIDENCE_CHANNEL_ACTUAL_PROVIDER = "actual_provider_channel"
EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK = "direct_signed_webhook_integration_probe"
EVIDENCE_CHANNEL_DIRECT_CODE_PROBE = "direct_code_probe"
EVIDENCE_CHANNELS = frozenset(
    {
        EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
        EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
        EVIDENCE_CHANNEL_DIRECT_CODE_PROBE,
    }
)

# Backward aliases used by the v1 manifest builder. They intentionally resolve
# to the corrected evidence vocabulary.
EXECUTION_PATH_REAL_CHANNEL_WEBHOOK = EVIDENCE_CHANNEL_ACTUAL_PROVIDER
EXECUTION_PATH_DIRECT_CODE_PROBE = EVIDENCE_CHANNEL_DIRECT_CODE_PROBE
EXECUTION_PATHS = EVIDENCE_CHANNELS

SESSION_STATE_STARTED = "started"
SESSION_STATE_AWAITING_DEVICE_SEND = "awaiting_device_send"
SESSION_STATE_OBSERVED = "observed"
SESSION_STATE_HUMAN_ASSESSED = "human_assessed"
SESSION_STATE_SCENARIO_COMPLETED = "scenario_completed"
SESSION_STATE_COMPLETED = "completed"
SESSION_STATE_TORN_DOWN = "torn_down"

SESSION_SCOPE_PHASE_ACCEPTANCE = "phase_acceptance"
SESSION_SCOPE_SINGLE_SCENARIO_RETEST = "single_scenario_retest"
SESSION_SCOPES = frozenset(
    {SESSION_SCOPE_PHASE_ACCEPTANCE, SESSION_SCOPE_SINGLE_SCENARIO_RETEST}
)

HUMAN_RUBRIC_VALUES = frozenset({"pass", "fail", "not_applicable"})

# ── Closed scenario taxonomy (minimum required categories) ───────────────────
SCENARIO_TAXONOMY: tuple[str, ...] = (
    "general_inquiry_faq",
    "catalog_search_availability",
    "multi_turn_order_construction",
    "interruption_topic_switch",
    "resume_after_interruption",
    "conditional_coupons_offers",
    "tracking_with_identifier",
    "tracking_without_identifier",
    "tracking_not_found",
    "tracking_success",
    "identity_profile_address_continuity",
    "memory_cross_turn",
    "memory_cross_conversation",
    "memory_tenant_boundary",
    "language_arabic",
    "language_english",
    "language_mixed",
    "voice_note_transcription",
    "media_image_handling",
    "audio_unsupported_corrupt",
    "tool_timeout",
    "tool_error_retry",
    "tool_idempotency",
    "payment_truth",
    "shipment_truth",
    "handoff_escalation",
    "pause_blocklist",
    "subscription_guard",
    "webhook_duplicate",
    "webhook_replay",
    "webhook_out_of_order",
    "sanitizer_guard",
    "dedup_guard",
    "cross_tenant_isolation",
    "cost_latency_budget",
)

# ── Provenance fields (constitutional metadata contract) ─────────────────────
PROVENANCE_FIELDS: tuple[str, ...] = (
    "compose_source",
    "response_mode",
    "chosen_path",
    "llm_candidate_present",
    "final_text_transformed",
    "final_transform_reasons",
    "fallback_reason",
    "fallback_action_type",
)

# ── Failure codes ────────────────────────────────────────────────────────────
CODE_COMMAND_INVALID = "command_invalid"
CODE_PROBE_FAILED = "probe_failed"
CODE_ACCEPTANCE_NOT_ENABLED = "acceptance_not_enabled"
CODE_EXECUTION_NOT_CONFIRMED = "execution_not_confirmed"
CODE_ARCH001_SIGNOFF_MISSING = "arch001_shadow_signoff_missing"
CODE_TENANT_1_NOT_PASSED = "tenant_1_not_passed"
CODE_STAGING_IDENTITY_REJECTED = "staging_identity_rejected"
CODE_RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"
CODE_RUNTIME_REVISION_UNKNOWN = "runtime_revision_unknown"
CODE_TARGET_APP_ROOT_REQUIRED = "target_app_root_required"
CODE_STORE_AI_MODE_INVALID = "store_ai_mode_invalid"
CODE_DATABASE_BINDING_REJECTED = "database_binding_rejected"
CODE_TENANT_SETTINGS_MISSING = "tenant_settings_missing"
CODE_DATABASE_QUERY_FAILED = "database_query_failed"
CODE_AI_TEST_ALLOWLIST_INVALID = "ai_test_allowlist_invalid"
CODE_APPROVED_EGRESS_CONFIGURATION_MISSING = "approved_egress_configuration_missing"
CODE_PHONE_NOT_ALLOWLISTED = "phone_not_allowlisted"
CODE_TENANT_NOT_ALLOWED = "tenant_not_allowed"
CODE_CHANNEL_HEALTH_BLOCKED = "channel_health_blocked"
CODE_PROVIDER_SANDBOX_UNAVAILABLE = "provider_sandbox_unavailable"
CODE_RATE_CAP_EXCEEDED = "rate_cap_exceeded"
CODE_MANIFEST_INVALID = "manifest_invalid"
CODE_REAL_CHANNEL_REQUIRED = "real_channel_required"
CODE_SESSION_NOT_FOUND = "session_not_found"
CODE_SESSION_STATE_INVALID = "session_state_invalid"
CODE_EVENT_CURSOR_STALE = "event_cursor_stale"
CODE_INBOUND_PROVIDER_ID_MISSING = "inbound_provider_id_missing"
CODE_INBOUND_PROVIDER_ID_REJECTED = "inbound_provider_id_rejected"
CODE_INBOUND_ORIGIN_REJECTED = "inbound_origin_rejected"
CODE_DEVICE_ATTESTATION_REQUIRED = "device_attestation_required"
CODE_OUTBOUND_PROVIDER_ID_MISSING = "outbound_provider_id_missing"
CODE_PROVENANCE_INCOMPLETE = "provenance_incomplete"
CODE_HUMAN_ASSESSMENT_REQUIRED = "human_assessment_required"
CODE_CONFIG_DRIFT = "config_drift"
CODE_TENANT_1_PASS_ARTIFACT_INVALID = "tenant_1_pass_artifact_invalid"
CODE_ORDER_SIDE_EFFECT_DETECTED = "order_side_effect_detected"
CODE_SCENARIO_NOT_IN_MANIFEST = "scenario_not_in_manifest"
CODE_SCENARIO_PHASE_MISMATCH = "scenario_phase_mismatch"

# ── Paths ────────────────────────────────────────────────────────────────────
MANIFEST_RELATIVE_PATH = Path("docs/engineering/real-channel-acceptance-scenario-manifest.json")
EVIDENCE_ACCUMULATION_DIR = Path("docs/engineering/staging-evidence")
DEFECT_BUNDLE_DIR = Path("docs/engineering/staging-evidence/defect-bundles")
SESSION_DEFAULT_DIR = Path(".nahla-acceptance-sessions")

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_PHONE_DIGITS_RE = re.compile(r"^\d{10,15}$")


def env_flag_enabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY_ENV


def hash_identifier(value: str, *, salt: str = "nahla-rca-v1") -> str:
    """One-way fingerprint for logs/evidence (never reversible phone/token)."""
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def hmac_identifier(value: str, *, key: str) -> str:
    """Keyed fingerprint for low-entropy identifiers such as phone numbers."""
    import hmac

    if not key:
        raise ValueError("evidence_hmac_key_missing")
    digest = hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest[:24]}"


def mask_phone_tail(value: str | None, *, keep: int = 4) -> str:
    if not value:
        return "-"
    digits = re.sub(r"\D", "", str(value))
    if len(digits) <= keep:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - keep)}{digits[-keep:]}"


def parse_allowlist_phones(raw: str | None) -> list[str]:
    if not raw:
        return []
    phones: list[str] = []
    for token in str(raw).split(","):
        digits = re.sub(r"\D", "", token.strip())
        if digits and _PHONE_DIGITS_RE.fullmatch(digits):
            phones.append(digits)
    return phones


def resolve_manifest_path(app_root: Path | None = None) -> Path:
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    return root / MANIFEST_RELATIVE_PATH


def load_scenario_manifest(app_root: Path | None = None) -> dict[str, Any]:
    path = resolve_manifest_path(app_root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(payload)
    return payload


ACCEPTANCE_PHASE_BY_TENANT: dict[int, str] = {
    TENANT_1_INTENSIVE: PHASE_TENANT_1_INTENSIVE,
    TENANT_33_LIMITED: PHASE_TENANT_33_LIMITED,
    TENANT_48_SALLA_MINIMAL: PHASE_TENANT_48_SALLA_MINIMAL,
}
ACCEPTANCE_TENANT_BY_PHASE: dict[str, int] = {
    phase: tenant_id for tenant_id, phase in ACCEPTANCE_PHASE_BY_TENANT.items()
}
PHASE_EXPECTED_SCENARIO_COUNTS: dict[str, int] = {
    PHASE_TENANT_1_INTENSIVE: 50,
    PHASE_TENANT_33_LIMITED: 16,
    PHASE_TENANT_48_SALLA_MINIMAL: 16,
}
PHASE_SCENARIO_ID_PREFIX: dict[str, str] = {
    PHASE_TENANT_1_INTENSIVE: "t1_",
    PHASE_TENANT_33_LIMITED: "t33_",
    PHASE_TENANT_48_SALLA_MINIMAL: "t48_",
}


def resolve_acceptance_phase(tenant_id: int) -> str:
    try:
        return ACCEPTANCE_PHASE_BY_TENANT[tenant_id]
    except KeyError as exc:
        raise ValueError(CODE_TENANT_NOT_ALLOWED) from exc


def validate_manifest(payload: Mapping[str, Any]) -> None:
    if payload.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(CODE_MANIFEST_INVALID)
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(CODE_MANIFEST_INVALID)

    manifest_phases = payload.get("phases")
    if not isinstance(manifest_phases, list) or not manifest_phases:
        raise ValueError(CODE_MANIFEST_INVALID)
    declared_phase_counts = payload.get("phase_scenario_counts")
    if not isinstance(declared_phase_counts, Mapping):
        raise ValueError(CODE_MANIFEST_INVALID)

    seen_manifest_phases: set[str] = set()
    for phase_row in manifest_phases:
        if not isinstance(phase_row, Mapping):
            raise ValueError(CODE_MANIFEST_INVALID)
        phase = str(phase_row.get("phase") or "")
        tenant_id = phase_row.get("tenant_id")
        if phase not in ACCEPTANCE_TENANT_BY_PHASE or tenant_id not in ACCEPTANCE_TENANTS:
            raise ValueError(CODE_MANIFEST_INVALID)
        if ACCEPTANCE_TENANT_BY_PHASE[phase] != tenant_id:
            raise ValueError(CODE_MANIFEST_INVALID)
        if phase in seen_manifest_phases:
            raise ValueError(CODE_MANIFEST_INVALID)
        seen_manifest_phases.add(phase)
        if phase == PHASE_TENANT_33_LIMITED and not phase_row.get("requires_tenant_1_pass"):
            raise ValueError(CODE_MANIFEST_INVALID)
        if phase == PHASE_TENANT_48_SALLA_MINIMAL and phase_row.get("requires_tenant_1_pass"):
            raise ValueError(CODE_MANIFEST_INVALID)
    if seen_manifest_phases != set(ACCEPTANCE_TENANT_BY_PHASE):
        raise ValueError(CODE_MANIFEST_INVALID)

    seen_ids: set[str] = set()
    taxonomy_seen: set[str] = set()
    phase_counts: dict[str, int] = {phase: 0 for phase in ACCEPTANCE_TENANT_BY_PHASE}
    phase_ids: dict[str, list[str]] = {phase: [] for phase in ACCEPTANCE_TENANT_BY_PHASE}
    for row in scenarios:
        if not isinstance(row, Mapping):
            raise ValueError(CODE_MANIFEST_INVALID)
        scenario_id = str(row.get("scenario_id") or "")
        if not scenario_id or scenario_id in seen_ids:
            raise ValueError(CODE_MANIFEST_INVALID)
        seen_ids.add(scenario_id)

        taxonomy = str(row.get("taxonomy") or "")
        if taxonomy not in SCENARIO_TAXONOMY:
            raise ValueError(CODE_MANIFEST_INVALID)
        taxonomy_seen.add(taxonomy)

        tenant_id = row.get("tenant_id")
        if tenant_id not in ACCEPTANCE_TENANTS:
            raise ValueError(CODE_MANIFEST_INVALID)

        phase = str(row.get("phase") or "")
        if phase not in ACCEPTANCE_TENANT_BY_PHASE:
            raise ValueError(CODE_MANIFEST_INVALID)
        if ACCEPTANCE_TENANT_BY_PHASE[phase] != tenant_id:
            raise ValueError(CODE_MANIFEST_INVALID)
        id_prefix = PHASE_SCENARIO_ID_PREFIX[phase]
        if not scenario_id.startswith(id_prefix):
            raise ValueError(CODE_MANIFEST_INVALID)
        phase_counts[phase] += 1
        phase_ids[phase].append(scenario_id)

        execution_path = str(row.get("execution_path") or "")
        if execution_path not in EXECUTION_PATHS:
            raise ValueError(CODE_MANIFEST_INVALID)

        for required_key in (
            "phase",
            "preconditions",
            "inbound",
            "expected_state",
            "allowed_conversational_variability",
            "prohibited_claims",
            "max_llm_calls",
            "max_tool_calls",
            "latency_budget_ms",
            "outbound_evidence",
            "cleanup",
            "eval_regression_mapping",
            "automation_class",
            "pass_fail_rubric",
            "device_action",
            "channel_evidence_required",
        ):
            if required_key not in row:
                raise ValueError(CODE_MANIFEST_INVALID)

    for phase, expected in PHASE_EXPECTED_SCENARIO_COUNTS.items():
        if phase_counts.get(phase) != expected:
            raise ValueError(CODE_MANIFEST_INVALID)
        if int(declared_phase_counts.get(phase) or -1) != expected:
            raise ValueError(CODE_MANIFEST_INVALID)
        if len(phase_ids[phase]) != len(set(phase_ids[phase])):
            raise ValueError(CODE_MANIFEST_INVALID)

    if not taxonomy_seen.issuperset(set(SCENARIO_TAXONOMY)):
        missing = sorted(set(SCENARIO_TAXONOMY) - taxonomy_seen)
        raise ValueError(f"{CODE_MANIFEST_INVALID}:missing_taxonomy:{','.join(missing)}")


def count_scenarios_by_phase(manifest: Mapping[str, Any]) -> dict[str, int]:
    counts = {
        PHASE_TENANT_1_INTENSIVE: 0,
        PHASE_TENANT_33_LIMITED: 0,
        PHASE_TENANT_48_SALLA_MINIMAL: 0,
        "total": 0,
    }
    for row in manifest.get("scenarios", []):
        phase = str(row.get("phase") or "")
        counts["total"] += 1
        if phase in counts:
            counts[phase] += 1
    return counts


def resolve_session_scenario_scope(
    manifest: Mapping[str, Any],
    *,
    phase: str,
    tenant_id: int,
    scenario_id: str | None = None,
) -> tuple[list[str], str]:
    """Resolve closed manifest scenario IDs and session scope for start-session."""
    if scenario_id is None:
        scenarios = [
            str(row["scenario_id"])
            for row in manifest.get("scenarios", [])
            if str(row.get("phase") or "") == phase
        ]
        return scenarios, SESSION_SCOPE_PHASE_ACCEPTANCE

    by_id = {
        str(row["scenario_id"]): row
        for row in manifest.get("scenarios", [])
        if row.get("scenario_id")
    }
    row = by_id.get(str(scenario_id))
    if row is None:
        raise ValueError(CODE_SCENARIO_NOT_IN_MANIFEST)
    if int(row.get("tenant_id") or -1) != tenant_id:
        raise ValueError(CODE_SCENARIO_PHASE_MISMATCH)
    if str(row.get("phase") or "") != phase:
        raise ValueError(CODE_SCENARIO_PHASE_MISMATCH)
    return [str(scenario_id)], SESSION_SCOPE_SINGLE_SCENARIO_RETEST


def required_config_snapshot_keys() -> tuple[str, ...]:
    return (
        "store_ai_mode",
        "store_ai_enabled",
        "ai_test_allowed_numbers_hash",
        "ai_paused",
        "handoff_active",
        "subscription_status",
        "blocklist_hash",
        "tenant_id",
        "pinned_revision",
        "correlation_id",
        "captured_at_utc",
    )


EXECUTION_GATE_REQUIRED_PROOFS: tuple[str, ...] = (
    "staging_identity",
    "database_binding",
    "arch001_signoff",
    "operator_channel_health",
    "allowlisted_test_number",
    "approved_egress_configuration",
)

_PHONE_ENV_BY_TENANT: dict[int, str] = {
    TENANT_1_INTENSIVE: TENANT_1_PHONE_ENV,
    TENANT_33_LIMITED: TENANT_33_PHONE_ENV,
    TENANT_48_SALLA_MINIMAL: TENANT_48_PHONE_ENV,
}

_PLATFORM_EGRESS_GUARD_KINDS: tuple[str, ...] = (
    "automation",
    "campaign",
    "external_tool",
    "financial",
    "salla_integration",
    "shipping",
    "whatsapp_provider",
)


def evaluate_approved_egress_configuration() -> dict[str, Any]:
    """Permanent fail-closed egress gate for real-channel execution (intentional).

    Platform egress guards (Moyasar, Salla, automations, shipping, WhatsApp)
    are enforced via ``deny_external_egress`` only under
    ``internal_conversational_e2e_context``. Real-channel acceptance cannot
    install that context without altering provider send behavior, so this proof
    **always** reports ``ok=False`` until a dedicated real-channel egress
    contract is approved and wired. This is policy, not an accidental gap.
    """
    return {
        "ok": False,
        "code": CODE_APPROVED_EGRESS_CONFIGURATION_MISSING,
        "policy": "permanent_fail_closed",
        "intentional_block": True,
        "internal_acceptance_context_installable": False,
        "reused_egress_guard_kinds": list(_PLATFORM_EGRESS_GUARD_KINDS),
        "note": (
            "Real-channel execution is intentionally blocked: internal acceptance "
            "context cannot be installed without altering channel behavior, and "
            "dedicated real-channel egress wiring is not yet approved"
        ),
    }


def _tenant_phone_env(tenant_id: int) -> str:
    try:
        return _PHONE_ENV_BY_TENANT[tenant_id]
    except KeyError as exc:
        raise ValueError(CODE_TENANT_NOT_ALLOWED) from exc


def evaluate_allowlisted_test_number(
    *,
    tenant_id: int,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = env or {}
    allowlist = parse_allowlist_phones(env.get(ALLOWLIST_PHONES_ENV))
    phone = re.sub(r"\D", "", str(env.get(_tenant_phone_env(tenant_id), "")))
    if not allowlist:
        return {
            "ok": False,
            "code": CODE_PHONE_NOT_ALLOWLISTED,
            "allowlist_count": 0,
            "test_phone_present": bool(phone),
        }
    if not phone or phone not in allowlist:
        return {
            "ok": False,
            "code": CODE_PHONE_NOT_ALLOWLISTED,
            "allowlist_count": len(allowlist),
            "test_phone_present": bool(phone),
        }
    return {
        "ok": True,
        "code": None,
        "allowlist_count": len(allowlist),
        "test_phone_hash": hash_identifier(phone),
    }


def evaluate_execution_gate_chain(
    *,
    tenant_id: int,
    env: Mapping[str, str] | None = None,
    arch001_signoff_ok: bool | None = None,
    channel_health_ok: bool | None = None,
) -> dict[str, Any]:
    """Fail closed unless every mandatory execution proof is present."""
    env_map = dict(env or os.environ)
    proofs: dict[str, bool] = {}
    blockers: list[str] = []

    identity_failure = validate_staging_identity(
        env_map,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    proofs["staging_identity"] = identity_failure is None
    if identity_failure is not None:
        blockers.append(CODE_STAGING_IDENTITY_REJECTED)

    binding_failure = validate_database_binding(env_map)
    proofs["database_binding"] = binding_failure is None
    if binding_failure is not None:
        blockers.append(CODE_DATABASE_BINDING_REJECTED)

    if arch001_signoff_ok is None:
        from scripts.operators.product_availability_preprod_synthetic_signoff_v2 import (  # noqa: PLC0415
            verify_arch001_preprod_signoff_for_gate,
        )

        signoff = verify_arch001_preprod_signoff_for_gate()
        arch001_signoff_ok = signoff.get("ok") is True
        if not arch001_signoff_ok:
            blockers.append(str(signoff.get("code") or CODE_ARCH001_SIGNOFF_MISSING))
    proofs["arch001_signoff"] = bool(arch001_signoff_ok)
    if not arch001_signoff_ok:
        if CODE_ARCH001_SIGNOFF_MISSING not in blockers:
            blockers.append(CODE_ARCH001_SIGNOFF_MISSING)

    proofs["operator_channel_health"] = channel_health_ok is True
    if channel_health_ok is not True:
        blockers.append(CODE_CHANNEL_HEALTH_BLOCKED)

    phone_proof = evaluate_allowlisted_test_number(tenant_id=tenant_id, env=env_map)
    proofs["allowlisted_test_number"] = phone_proof.get("ok") is True
    if not proofs["allowlisted_test_number"]:
        blockers.append(str(phone_proof.get("code") or CODE_PHONE_NOT_ALLOWLISTED))

    egress_proof = evaluate_approved_egress_configuration()
    proofs["approved_egress_configuration"] = egress_proof.get("ok") is True
    if not proofs["approved_egress_configuration"]:
        blockers.append(CODE_APPROVED_EGRESS_CONFIGURATION_MISSING)

    return {
        "ok": not blockers,
        "proofs": proofs,
        "required_proofs": list(EXECUTION_GATE_REQUIRED_PROOFS),
        "blockers": sorted(set(blockers)),
        "approved_egress_policy": egress_proof.get("policy"),
        "approved_egress_intentional_block": egress_proof.get("intentional_block"),
    }


def read_only_preflight_db_io_permitted(
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Return whether read-only preflight DB I/O is allowed."""
    env_map = dict(env or os.environ)
    if not env_flag_enabled(env_map.get(MASTER_ENABLE_ENV)):
        return False, CODE_ACCEPTANCE_NOT_ENABLED
    identity_failure = validate_staging_identity(
        env_map,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if identity_failure is not None:
        return False, CODE_STAGING_IDENTITY_REJECTED
    binding_failure = validate_database_binding(env_map)
    if binding_failure is not None:
        return False, CODE_DATABASE_BINDING_REJECTED
    if not str(env_map.get("DATABASE_URL") or "").strip():
        return False, CODE_DATABASE_BINDING_REJECTED
    return True, None


def config_snapshot_db_io_permitted(
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Return whether read-only config snapshot DB I/O is allowed."""
    return read_only_preflight_db_io_permitted(env)


__all__ = [
    "ACCEPTANCE_CUTOVER_LABEL",
    "ACCEPTANCE_CUTOVER_SCOPE",
    "ACCEPTANCE_PHASE_BY_TENANT",
    "ACCEPTANCE_TARGET_PROVIDER_PATH",
    "ACCEPTANCE_TENANT_BY_PHASE",
    "ACCEPTANCE_TENANTS",
    "ALLOWLIST_PHONES_ENV",
    "ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST_ENV",
    "ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID_ENV",
    "ARCH001_PREPROD_ISOLATED_SERVICE_ID_ENV",
    "ARCH001_PREPROD_ISOLATED_SERVICE_NAME_ENV",
    "ARCH001_PREPROD_PINNED_REVISION_ENV",
    "ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV",
    "ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV",
    "ARCH001_SHADOW_SIGNOFF_ENV",
    "CHANNEL_PREFLIGHT_ENV_NAMES",
    "CODE_ACCEPTANCE_NOT_ENABLED",
    "CODE_ARCH001_SIGNOFF_MISSING",
    "CODE_CHANNEL_D360_ONLY_LEGACY_PATH",
    "CODE_CHANNEL_HEALTH_BLOCKED",
    "CODE_CHANNEL_READINESS_GAP",
    "CODE_COMMAND_INVALID",
    "CODE_CONFIG_DRIFT",
    "CODE_AI_TEST_ALLOWLIST_INVALID",
    "CODE_APPROVED_EGRESS_CONFIGURATION_MISSING",
    "CODE_DATABASE_BINDING_REJECTED",
    "CODE_DATABASE_QUERY_FAILED",
    "CODE_TENANT_SETTINGS_MISSING",
    "CODE_DB_WA_BINDING_INVALID",
    "CODE_DB_WA_BINDING_MISMATCH",
    "CODE_DB_WA_BINDING_MISSING",
    "CODE_DEVICE_ATTESTATION_REQUIRED",
    "CODE_EVENT_CURSOR_STALE",
    "CODE_EXECUTION_NOT_CONFIRMED",
    "CODE_HUMAN_ASSESSMENT_REQUIRED",
    "CODE_INBOUND_ORIGIN_REJECTED",
    "CODE_INBOUND_PROVIDER_ID_MISSING",
    "CODE_INBOUND_PROVIDER_ID_REJECTED",
    "CODE_MANIFEST_INVALID",
    "CODE_OUTBOUND_PROVIDER_ID_MISSING",
    "CODE_PHONE_NOT_ALLOWLISTED",
    "CODE_PROBE_FAILED",
    "CODE_PROVIDER_SANDBOX_UNAVAILABLE",
    "CODE_PROVENANCE_INCOMPLETE",
    "CODE_RATE_CAP_EXCEEDED",
    "CODE_ROLLBACK_SNAPSHOT_INVALID",
    "CODE_ROLLBACK_SNAPSHOT_MISSING",
    "CODE_ROLLBACK_SNAPSHOT_STALE",
    "CODE_RUNTIME_REVISION_MISMATCH",
    "CODE_RUNTIME_REVISION_UNKNOWN",
    "CODE_SESSION_NOT_FOUND",
    "CODE_SESSION_STATE_INVALID",
    "CODE_STAGING_IDENTITY_REJECTED",
    "CODE_STORE_AI_MODE_INVALID",
    "CODE_TARGET_APP_ROOT_REQUIRED",
    "CODE_TENANT_1_NOT_PASSED",
    "CODE_TENANT_1_PASS_ARTIFACT_INVALID",
    "CODE_TENANT_NOT_ALLOWED",
    "CODE_WEBHOOK_ATTESTATION_BACKEND_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_DEPLOYMENT_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_FORGED",
    "CODE_WEBHOOK_ATTESTATION_INVALID",
    "CODE_WEBHOOK_ATTESTATION_MISSING",
    "CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH",
    "CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED",
    "CODE_WEBHOOK_ATTESTATION_STALE",
    "CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH",
    "CODE_WEBHOOK_OBSERVATION_INVALID",
    "CODE_WEBHOOK_OBSERVATION_STALE",
    "D360_LEGACY_OBSERVABILITY_ENV_NAMES",
    "DEFAULT_SCENARIO_LATENCY_BUDGET_MS",
    "DEFAULT_SCENARIO_MAX_LLM_CALLS",
    "DEFAULT_SCENARIO_MAX_TOOL_CALLS",
    "DEFECT_BUNDLE_DIR",
    "EVIDENCE_ACCUMULATION_DIR",
    "EVIDENCE_CHANNEL_ACTUAL_PROVIDER",
    "EVIDENCE_CHANNEL_DIRECT_CODE_PROBE",
    "EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK",
    "EVIDENCE_CHANNELS",
    "EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK",
    "EVIDENCE_HMAC_KEY_ENV",
    "EXECUTION_GATE_REQUIRED_PROOFS",
    "EVIDENCE_SCHEMA_VERSION",
    "EXECUTION_CONFIRM_ENV",
    "EXECUTION_PATH_DIRECT_CODE_PROBE",
    "EXECUTION_PATH_REAL_CHANNEL_WEBHOOK",
    "EXECUTION_PATHS",
    "GATE_PHASES",
    "HUMAN_RUBRIC_VALUES",
    "LEGACY_PROVIDER_PATH",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_SCHEMA_VERSION",
    "MASTER_ENABLE_ENV",
    "MAX_INBOUND_MESSAGES_PER_SESSION",
    "MAX_LLM_CALLS_PER_SESSION",
    "MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION",
    "MAX_SCENARIOS_PER_SESSION",
    "MAX_SESSION_COST_USD",
    "META_DIRECT_WEBHOOK_ROUTE",
    "META_ONBOARDING_EXTERNAL_BLOCKER",
    "META_ONBOARDING_TARGET_PATH",
    "META_READINESS_REQUIRED_ENV_NAMES",
    "PHASE_ARCH001_SHADOW_SIGNOFF_GATE",
    "PHASE_CHANNEL_HEALTH",
    "PHASE_CONFIG_SNAPSHOT",
    "PHASE_DEFAULT_OFF",
    "PHASE_DEFECT_BUNDLE",
    "PHASE_EXPECTED_SCENARIO_COUNTS",
    "PHASE_READINESS_PREFLIGHT",
    "PHASE_RUNTIME_REVISION_ATTESTATION",
    "PHASE_SCENARIO_ID_PREFIX",
    "PHASE_SUMMARY",
    "PHASE_TEARDOWN",
    "PHASE_TENANT_1_INTENSIVE",
    "PHASE_TENANT_33_LIMITED",
    "PHASE_TENANT_48_SALLA_MINIMAL",
    "PINNED_REVISION_ENV",
    "PROVENANCE_FIELDS",
    "REPORT_SCHEMA_VERSION",
    "REVIEWER_ID_ENV",
    "SCENARIO_TAXONOMY",
    "SESSION_DEFAULT_DIR",
    "SESSION_DIR_ENV",
    "SESSION_SCHEMA_VERSION",
    "SESSION_SCOPE_PHASE_ACCEPTANCE",
    "SESSION_SCOPE_SINGLE_SCENARIO_RETEST",
    "SESSION_SCOPES",
    "SESSION_STATE_AWAITING_DEVICE_SEND",
    "SESSION_STATE_COMPLETED",
    "SESSION_STATE_HUMAN_ASSESSED",
    "SESSION_STATE_OBSERVED",
    "SESSION_STATE_SCENARIO_COMPLETED",
    "SESSION_STATE_STARTED",
    "SESSION_STATE_TORN_DOWN",
    "STAGING_DB_WA_BINDING_PROVIDER_VALUE",
    "STAGING_DB_WA_BINDING_REQUIRED_FIELDS",
    "STAGING_ENVIRONMENT_ENV",
    "STAGING_ENVIRONMENT_VALUE",
    "STAGING_IDENTITY_CLASS",
    "STAGING_PROJECT_ENV",
    "STAGING_PROJECT_VALUE",
    "TENANT_1_ACCEPTANCE_CUTOVER_TENANT_ID",
    "TENANT_1_CONVERSATION_ENV",
    "TENANT_1_INTENSIVE",
    "TENANT_1_PASS_ARTIFACT_ENV",
    "TENANT_1_PASS_CONFIRM_ENV",
    "TENANT_1_PHONE_ENV",
    "TENANT_33_CONVERSATION_ENV",
    "TENANT_33_LIMITED",
    "TENANT_33_PHONE_ENV",
    "TENANT_48_CONVERSATION_ENV",
    "TENANT_48_PHONE_ENV",
    "TENANT_48_SALLA_MINIMAL",
    "WEBHOOK_ATTESTATION_ARTIFACT_ENV",
    "WEBHOOK_ATTESTATION_HMAC_KEY_ENV",
    "build_acceptance_cutover_guidance",
    "config_snapshot_db_io_permitted",
    "count_scenarios_by_phase",
    "env_flag_enabled",
    "evaluate_allowlisted_test_number",
    "evaluate_approved_egress_configuration",
    "evaluate_execution_gate_chain",
    "evaluate_meta_channel_readiness",
    "evaluate_meta_config_present",
    "evaluate_operator_attested_channel_ready",
    "hash_identifier",
    "hmac_identifier",
    "is_d360_only_legacy_path",
    "load_scenario_manifest",
    "mask_phone_tail",
    "parse_allowlist_phones",
    "read_only_preflight_db_io_permitted",
    "required_config_snapshot_keys",
    "resolve_acceptance_phase",
    "resolve_manifest_path",
    "resolve_session_scenario_scope",
    "validate_manifest",
]
