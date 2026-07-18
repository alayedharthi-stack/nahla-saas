"""Closed contract for product availability truth guard shadow observation."""
from __future__ import annotations

REPORT_SCHEMA_VERSION = "product_availability_shadow_observation_v1"
SHADOW_MODE_ENV = "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"
ENFORCE_MODE_VALUE = "enforce"
SHADOW_MODE_VALUE = "shadow"

DEPLOYMENT_APP_ROOT = "/app"
APP_CONTAINER_SYS_PATH = (
    f"{DEPLOYMENT_APP_ROOT}",
    f"{DEPLOYMENT_APP_ROOT}/backend",
    f"{DEPLOYMENT_APP_ROOT}/database",
)

CODE_SHADOW_MODE_NOT_ENABLED = "shadow_mode_not_enabled"
CODE_ENFORCE_MODE_ENABLED = "enforce_mode_enabled"
CODE_COMMAND_INVALID = "command_invalid"
CODE_PROBE_FAILED = "probe_failed"
CODE_RUNTIME_EXECUTION_REQUIRED = "runtime_execution_required"
CODE_ARTIFACT_MANIFEST_MISMATCH = "artifact_manifest_mismatch"

# Generic cross-category synthetic fixtures — never merchant-specific.
FIXTURE_TENANT_A = 91001
FIXTURE_TENANT_B = 91002

OBSERVATION_WINDOW_HOURS = 48
MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES = 0
MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS = 0
MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS = 0
MAX_ACCEPTABLE_OUTBOUND_PROVIDER_CALLS = 0

PHASE_DEFAULT_OFF = "default_off_verify"
PHASE_SYNTHETIC_MATRIX = "synthetic_matrix"
PHASE_RUNTIME_REVISION_ATTESTATION = "runtime_revision_attestation"
PHASE_SUMMARY = "summary"
PHASE_TEARDOWN = "teardown_flags"
PHASE_ARTIFACT_MANIFEST = "artifact_manifest"
PHASE_RUNTIME_MATRIX = "runtime_synthetic_matrix"

# Closed runtime inventory.  The digest binds both the operator and every
# production hook/module needed for ARCH-001 shadow observations.
RUNTIME_ARTIFACT_PATHS = (
    "scripts/operators/product_availability_truth_guard_shadow_observation.py",
    "scripts/operators/product_availability_truth_guard_shadow_observation_contract.py",
    "backend/modules/ai/brain/postprocess/product_availability_truth_guard.py",
    "backend/modules/ai/brain/postprocess/product_availability_shadow_telemetry.py",
    "backend/modules/ai/brain/pipeline.py",
    "backend/routers/whatsapp_webhook.py",
)

__all__ = [
    "APP_CONTAINER_SYS_PATH",
    "CODE_COMMAND_INVALID",
    "CODE_ENFORCE_MODE_ENABLED",
    "CODE_PROBE_FAILED",
    "CODE_RUNTIME_EXECUTION_REQUIRED",
    "CODE_ARTIFACT_MANIFEST_MISMATCH",
    "CODE_SHADOW_MODE_NOT_ENABLED",
    "DEPLOYMENT_APP_ROOT",
    "ENFORCE_MODE_VALUE",
    "FIXTURE_TENANT_A",
    "FIXTURE_TENANT_B",
    "MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS",
    "MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES",
    "MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS",
    "MAX_ACCEPTABLE_OUTBOUND_PROVIDER_CALLS",
    "OBSERVATION_WINDOW_HOURS",
    "PHASE_DEFAULT_OFF",
    "PHASE_RUNTIME_REVISION_ATTESTATION",
    "PHASE_SUMMARY",
    "PHASE_SYNTHETIC_MATRIX",
    "PHASE_TEARDOWN",
    "PHASE_ARTIFACT_MANIFEST",
    "PHASE_RUNTIME_MATRIX",
    "REPORT_SCHEMA_VERSION",
    "RUNTIME_ARTIFACT_PATHS",
    "SHADOW_MODE_ENV",
    "SHADOW_MODE_VALUE",
]
