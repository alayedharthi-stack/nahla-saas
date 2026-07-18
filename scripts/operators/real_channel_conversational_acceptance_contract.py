"""Closed contract for post-shadow real-channel conversational acceptance (ARCH-001).

Default-off preparation only. No tenant activation, no outbound channel messages,
and no secrets in repository artifacts. Test identities resolve from operator env
at execution time.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

REPORT_SCHEMA_VERSION = "real_channel_conversational_acceptance_v1"
MANIFEST_SCHEMA_VERSION = "real_channel_acceptance_scenario_manifest_v1"
EVIDENCE_SCHEMA_VERSION = "real_channel_acceptance_evidence_v1"

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
TENANT_1_PASS_CONFIRM_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_CONFIRM"

# ── Secret-backed test identity refs (names only; values never committed) ───
PINNED_REVISION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION"
TENANT_1_PHONE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE"
TENANT_33_PHONE_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE"
ALLOWLIST_PHONES_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_ALLOWLIST_PHONES"
TENANT_1_CONVERSATION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_CONVERSATION_ID"
TENANT_33_CONVERSATION_ENV = "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_CONVERSATION_ID"

# ── Channel / provider preflight env (existence checked; values never logged) ─
CHANNEL_PREFLIGHT_ENV_NAMES: tuple[str, ...] = (
    "DATABASE_URL",
    "BACKEND_URL",
    "D360_API_BASE_URL",
    "D360_PARTNER_HUB_BASE",
    "D360_PARTNER_API_KEY",
    "D360_PARTNER_ID",
    "META_APP_SECRET",
    "WHATSAPP_API_URL",
    "WHATSAPP_TOKEN",
    "WHATSAPP_VERIFY_TOKEN",
)

# ── Tenants (platform-wide; no merchant-specific runtime logic) ────────────────
TENANT_1_INTENSIVE = 1
TENANT_33_LIMITED = 33
ACCEPTANCE_TENANTS = frozenset({TENANT_1_INTENSIVE, TENANT_33_LIMITED})

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
        PHASE_DEFECT_BUNDLE,
        PHASE_TEARDOWN,
    }
)

# ── Execution path taxonomy ──────────────────────────────────────────────────
EXECUTION_PATH_REAL_CHANNEL_WEBHOOK = "real_channel_webhook"
EXECUTION_PATH_DIRECT_CODE_PROBE = "direct_code_probe"
EXECUTION_PATHS = frozenset(
    {EXECUTION_PATH_REAL_CHANNEL_WEBHOOK, EXECUTION_PATH_DIRECT_CODE_PROBE}
)

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
CODE_PHONE_NOT_ALLOWLISTED = "phone_not_allowlisted"
CODE_TENANT_NOT_ALLOWED = "tenant_not_allowed"
CODE_CHANNEL_HEALTH_BLOCKED = "channel_health_blocked"
CODE_PROVIDER_SANDBOX_UNAVAILABLE = "provider_sandbox_unavailable"
CODE_RATE_CAP_EXCEEDED = "rate_cap_exceeded"
CODE_MANIFEST_INVALID = "manifest_invalid"
CODE_REAL_CHANNEL_REQUIRED = "real_channel_required"

# ── Paths ────────────────────────────────────────────────────────────────────
MANIFEST_RELATIVE_PATH = Path("docs/engineering/real-channel-acceptance-scenario-manifest.json")
EVIDENCE_ACCUMULATION_DIR = Path("docs/engineering/staging-evidence")
DEFECT_BUNDLE_DIR = Path("docs/engineering/staging-evidence/defect-bundles")

_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})
_PHONE_DIGITS_RE = re.compile(r"^\d{10,15}$")


def env_flag_enabled(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY_ENV


def hash_identifier(value: str, *, salt: str = "nahla-rca-v1") -> str:
    """One-way fingerprint for logs/evidence (never reversible phone/token)."""
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


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


def validate_manifest(payload: Mapping[str, Any]) -> None:
    if payload.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(CODE_MANIFEST_INVALID)
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError(CODE_MANIFEST_INVALID)

    seen_ids: set[str] = set()
    taxonomy_seen: set[str] = set()
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
        ):
            if required_key not in row:
                raise ValueError(CODE_MANIFEST_INVALID)

    if not taxonomy_seen.issuperset(set(SCENARIO_TAXONOMY)):
        missing = sorted(set(SCENARIO_TAXONOMY) - taxonomy_seen)
        raise ValueError(f"{CODE_MANIFEST_INVALID}:missing_taxonomy:{','.join(missing)}")


def count_scenarios_by_phase(manifest: Mapping[str, Any]) -> dict[str, int]:
    counts = {"tenant_1_intensive": 0, "tenant_33_limited": 0, "total": 0}
    for row in manifest.get("scenarios", []):
        phase = str(row.get("phase") or "")
        counts["total"] += 1
        if phase == PHASE_TENANT_1_INTENSIVE:
            counts["tenant_1_intensive"] += 1
        elif phase == PHASE_TENANT_33_LIMITED:
            counts["tenant_33_limited"] += 1
    return counts


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


__all__ = [
    "ACCEPTANCE_TENANTS",
    "ALLOWLIST_PHONES_ENV",
    "ARCH001_SHADOW_SIGNOFF_ENV",
    "CHANNEL_PREFLIGHT_ENV_NAMES",
    "CODE_ACCEPTANCE_NOT_ENABLED",
    "CODE_ARCH001_SIGNOFF_MISSING",
    "CODE_CHANNEL_HEALTH_BLOCKED",
    "CODE_COMMAND_INVALID",
    "CODE_EXECUTION_NOT_CONFIRMED",
    "CODE_MANIFEST_INVALID",
    "CODE_PHONE_NOT_ALLOWLISTED",
    "CODE_PROBE_FAILED",
    "CODE_PROVIDER_SANDBOX_UNAVAILABLE",
    "CODE_RATE_CAP_EXCEEDED",
    "CODE_REAL_CHANNEL_REQUIRED",
    "CODE_RUNTIME_REVISION_MISMATCH",
    "CODE_RUNTIME_REVISION_UNKNOWN",
    "CODE_STAGING_IDENTITY_REJECTED",
    "CODE_STORE_AI_MODE_INVALID",
    "CODE_TARGET_APP_ROOT_REQUIRED",
    "CODE_TENANT_1_NOT_PASSED",
    "CODE_TENANT_NOT_ALLOWED",
    "DEFAULT_SCENARIO_LATENCY_BUDGET_MS",
    "DEFAULT_SCENARIO_MAX_LLM_CALLS",
    "DEFAULT_SCENARIO_MAX_TOOL_CALLS",
    "DEFECT_BUNDLE_DIR",
    "EVIDENCE_ACCUMULATION_DIR",
    "EVIDENCE_SCHEMA_VERSION",
    "EXECUTION_CONFIRM_ENV",
    "EXECUTION_PATH_DIRECT_CODE_PROBE",
    "EXECUTION_PATH_REAL_CHANNEL_WEBHOOK",
    "EXECUTION_PATHS",
    "GATE_PHASES",
    "MANIFEST_RELATIVE_PATH",
    "MANIFEST_SCHEMA_VERSION",
    "MASTER_ENABLE_ENV",
    "MAX_INBOUND_MESSAGES_PER_SESSION",
    "MAX_LLM_CALLS_PER_SESSION",
    "MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION",
    "MAX_SCENARIOS_PER_SESSION",
    "MAX_SESSION_COST_USD",
    "PHASE_ARCH001_SHADOW_SIGNOFF_GATE",
    "PHASE_CHANNEL_HEALTH",
    "PHASE_CONFIG_SNAPSHOT",
    "PHASE_DEFAULT_OFF",
    "PHASE_DEFECT_BUNDLE",
    "PHASE_READINESS_PREFLIGHT",
    "PHASE_RUNTIME_REVISION_ATTESTATION",
    "PHASE_SUMMARY",
    "PHASE_TEARDOWN",
    "PHASE_TENANT_1_INTENSIVE",
    "PHASE_TENANT_33_LIMITED",
    "PINNED_REVISION_ENV",
    "PROVENANCE_FIELDS",
    "REPORT_SCHEMA_VERSION",
    "SCENARIO_TAXONOMY",
    "STAGING_ENVIRONMENT_ENV",
    "STAGING_ENVIRONMENT_VALUE",
    "STAGING_IDENTITY_CLASS",
    "STAGING_PROJECT_ENV",
    "STAGING_PROJECT_VALUE",
    "TENANT_1_CONVERSATION_ENV",
    "TENANT_1_INTENSIVE",
    "TENANT_1_PASS_CONFIRM_ENV",
    "TENANT_1_PHONE_ENV",
    "TENANT_33_CONVERSATION_ENV",
    "TENANT_33_LIMITED",
    "TENANT_33_PHONE_ENV",
    "count_scenarios_by_phase",
    "env_flag_enabled",
    "hash_identifier",
    "load_scenario_manifest",
    "mask_phone_tail",
    "parse_allowlist_phones",
    "required_config_snapshot_keys",
    "resolve_manifest_path",
    "validate_manifest",
]
