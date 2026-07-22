"""Staging real-channel acceptance config consolidation operator (default-off).

Consolidates channel/config from ``nahla-saas-staging`` onto canonical ``nahla-saas``
within ``desirable-growth`` / ``staging``. Safe for CI (mocked Railway). No Railway
mutations unless ARCH-001 shadow is torn down and all gates pass.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
    normalize_revision_token,
)
from scripts.operators.product_availability_preprod_synthetic_signoff_v2 import (
    verify_arch001_preprod_signoff_for_gate,
)
from scripts.operators.staging_acceptance_config_consolidation_contract import (
    APPLY_CONFIRM_ENV,
    APPLY_CONFIRM_TOKEN,
    ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV,
    ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV,
    ARCH001_SAFE_VALUES,
    ARCH001_SHADOW_ACTIVE_VALUE,
    ARCH001_SHADOW_MODE_ENV,
    ARCH001_SHADOW_SIGNOFF_ENV,
    ARCH001_TEARDOWN_PROOF_ENV,
    CANONICAL_SERVICE_ID,
    CANONICAL_SERVICE_NAME,
    CODE_APPLY_NOT_CONFIRMED,
    CODE_ARCH001_SHADOW_ACTIVE,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_ARCH001_TEARDOWN_PROOF_MISSING,
    CODE_CHANNEL_D360_ONLY_LEGACY_PATH,
    CODE_CHANNEL_READINESS_GAP,
    CODE_COMMAND_INVALID,
    CODE_CONFLICT_DETECTED,
    CODE_CONSOLIDATION_NOT_ENABLED,
    CODE_DEPLOY_ATTESTATION_FAILED,
    CODE_PRODUCTION_REJECTED,
    CODE_PROTECTED_VARIABLE_TOUCH,
    CODE_RAILWAY_ALLOWLIST_REJECTED,
    CODE_REFERENCE_BINDING_REJECTED,
    CODE_RUNTIME_REVISION_MISMATCH,
    CODE_SIGNATURE_WEAKENING,
    CODE_SNAPSHOT_INVALID,
    CODE_SNAPSHOT_KEY_MISSING,
    CODE_STAGING_IDENTITY_REJECTED,
    FORBIDDEN_ENVIRONMENT_MARKERS,
    FORBIDDEN_SERVICE_NAMES,
    FORBIDDEN_SIGNATURE_WEAKENING,
    LEGACY_SOURCE_SERVICE_ID,
    LEGACY_SOURCE_SERVICE_NAME,
    MASTER_ENABLE_ENV,
    MIGRATABLE_VARIABLE_KEYS,
    PHASE_APPLY,
    PHASE_ARCH001_SHADOW_BLOCK,
    PHASE_ARCH001_TEARDOWN_PROOF,
    PHASE_CONFLICT_DETECTION,
    PHASE_DEFAULT_OFF,
    PHASE_DRY_RUN_PLAN,
    PHASE_INVENTORY,
    PHASE_RAILWAY_ALLOWLIST,
    PHASE_ROLLBACK,
    PHASE_ROUTING_SELECTION,
    PHASE_SNAPSHOT,
    PHASE_STAGING_IDENTITY,
    PHASE_SUMMARY,
    PHASE_VERIFY,
    PINNED_REVISION_ENV,
    PROTECTED_VARIABLE_KEYS,
    REFERENCE_BINDABLE_KEYS,
    REPORT_SCHEMA_VERSION,
    SIGNATURE_MODE_KEYS,
    SNAPSHOT_DIR_ENV,
    SNAPSHOT_KEY_ENV,
    SNAPSHOT_SCHEMA_VERSION,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_ID_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_ID_ENV,
    STAGING_PROJECT_VALUE,
    STAGING_RAILWAY_ENVIRONMENT_ID,
    STAGING_RAILWAY_PROJECT_ID,
    ACCEPTANCE_TARGET_PROVIDER_PATH,
    META_DIRECT_WEBHOOK_ROUTE,
    META_ONBOARDING_EXTERNAL_BLOCKER,
    META_ONBOARDING_TARGET_PATH,
    all_migratable_keys,
    build_acceptance_cutover_guidance,
    build_migration_plan,
    detect_conflicts,
    env_flag_enabled,
    evaluate_meta_channel_readiness,
    fingerprint_value,
    is_reference_bindable,
    presence_only,
    validate_railway_identity,
)


@dataclass(frozen=True)
class ServiceSnapshot:
    service_id: str
    service_name: str
    variables: dict[str, str]
    deployment_id: str
    source_revision: str
    domains: tuple[str, ...]
    routes: tuple[str, ...]


@dataclass(frozen=True)
class RailwayObservation:
    project_id: str
    environment_id: str
    canonical: ServiceSnapshot
    legacy_source: ServiceSnapshot
    staging_db_wa_binding: dict[str, str] | None = None
    acceptance_cutover_snapshot: dict[str, Any] | None = None
    require_tenant_1_acceptance_cutover: bool = False


class RailwayReader(Protocol):
    def observe_staging_services(self) -> RailwayObservation: ...


class RailwayMutator(Protocol):
    def set_service_variables(self, *, service_id: str, variables: Mapping[str, str]) -> None: ...
    def trigger_deploy(self, *, service_id: str) -> str: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "captured_at_utc": _utc_now(),
        **payload,
    }


def _reject_production_markers(*, project: str, environment: str, service_names: tuple[str, ...]) -> str | None:
    env_lower = environment.lower()
    for marker in FORBIDDEN_ENVIRONMENT_MARKERS:
        if marker in env_lower:
            return "production_environment_marker"
    for name in service_names:
        if name in FORBIDDEN_SERVICE_NAMES:
            return "forbidden_service_name"
        lowered = name.lower()
        for marker in FORBIDDEN_ENVIRONMENT_MARKERS:
            if marker in lowered:
                return "production_service_marker"
    if project != STAGING_PROJECT_VALUE:
        return "project_name_not_staging"
    if env_lower != STAGING_ENVIRONMENT_VALUE:
        return "environment_name_not_staging"
    return None


def execute_default_off_probe() -> dict[str, Any]:
    enabled = env_flag_enabled(os.environ.get(MASTER_ENABLE_ENV))
    return _report(
        PHASE_DEFAULT_OFF,
        ok=not enabled,
        consolidation_enabled=enabled,
        canonical_service=CANONICAL_SERVICE_NAME,
        legacy_source_service=LEGACY_SOURCE_SERVICE_NAME,
        message=(
            "consolidation_disabled_by_default"
            if not enabled
            else "consolidation_enabled_requires_full_gate_chain"
        ),
    )


def gate_staging_identity(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    from scripts.operators.staging_migration_operator_gates import validate_staging_identity

    env = env or os.environ
    failure = validate_staging_identity(
        env,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if failure is not None:
        return _report(
            PHASE_STAGING_IDENTITY,
            ok=False,
            code=CODE_STAGING_IDENTITY_REJECTED,
            staging_identity_class=STAGING_IDENTITY_CLASS,
            error_class=failure.error_class,
            stage=failure.stage,
        )
    production_reject = _reject_production_markers(
        project=(env.get(STAGING_PROJECT_ENV) or "").strip(),
        environment=(env.get(STAGING_ENVIRONMENT_ENV) or "").strip(),
        service_names=(CANONICAL_SERVICE_NAME, LEGACY_SOURCE_SERVICE_NAME),
    )
    if production_reject:
        return _report(
            PHASE_STAGING_IDENTITY,
            ok=False,
            code=CODE_PRODUCTION_REJECTED,
            stage=production_reject,
        )
    return _report(
        PHASE_STAGING_IDENTITY,
        ok=True,
        staging_identity_class=STAGING_IDENTITY_CLASS,
        project_name=STAGING_PROJECT_VALUE,
        environment_name=STAGING_ENVIRONMENT_VALUE,
    )


def gate_railway_allowlist(observation: RailwayObservation) -> dict[str, Any]:
    production_reject = _reject_production_markers(
        project=STAGING_PROJECT_VALUE,
        environment=STAGING_ENVIRONMENT_VALUE,
        service_names=(observation.canonical.service_name, observation.legacy_source.service_name),
    )
    if production_reject:
        return _report(PHASE_RAILWAY_ALLOWLIST, ok=False, code=CODE_PRODUCTION_REJECTED, stage=production_reject)

    for service in (observation.canonical, observation.legacy_source):
        reason = validate_railway_identity(
            project_id=observation.project_id,
            environment_id=observation.environment_id,
            service_id=service.service_id,
            service_name=service.service_name,
        )
        if reason:
            return _report(
                PHASE_RAILWAY_ALLOWLIST,
                ok=False,
                code=CODE_RAILWAY_ALLOWLIST_REJECTED,
                service_name=service.service_name,
                stage=reason,
            )
    return _report(
        PHASE_RAILWAY_ALLOWLIST,
        ok=True,
        project_id=observation.project_id,
        environment_id=observation.environment_id,
        canonical_service_id=observation.canonical.service_id,
        legacy_source_service_id=observation.legacy_source.service_id,
    )


def gate_arch001_shadow_block(observation: RailwayObservation) -> dict[str, Any]:
    mode = (observation.canonical.variables.get(ARCH001_SHADOW_MODE_ENV) or "off").strip().lower()
    shadow_active = mode == ARCH001_SHADOW_ACTIVE_VALUE
    return _report(
        PHASE_ARCH001_SHADOW_BLOCK,
        ok=not shadow_active,
        code=CODE_ARCH001_SHADOW_ACTIVE if shadow_active else None,
        arch001_mode=mode if mode in ARCH001_SAFE_VALUES or mode == ARCH001_SHADOW_ACTIVE_VALUE else "unknown",
        note="ARCH-001 shadow must be off before consolidation apply",
    )


def gate_arch001_teardown_proof(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    proof = (env.get(ARCH001_TEARDOWN_PROOF_ENV) or "").strip()
    signoff = verify_arch001_preprod_signoff_for_gate(env=env)
    if not proof:
        return _report(
            PHASE_ARCH001_TEARDOWN_PROOF,
            ok=False,
            code=CODE_ARCH001_TEARDOWN_PROOF_MISSING,
            arch001_preprod_signoff_v2_valid=signoff.get("ok") is True,
        )
    if signoff.get("ok") is not True:
        return _report(
            PHASE_ARCH001_TEARDOWN_PROOF,
            ok=False,
            code=CODE_ARCH001_SIGNOFF_MISSING,
            arch001_preprod_signoff_v2_valid=False,
            blockers=signoff.get("blockers") or [],
        )
    return _report(
        PHASE_ARCH001_TEARDOWN_PROOF,
        ok=True,
        arch001_teardown_proof_ref=proof,
        arch001_preprod_signoff_v2_valid=True,
    )


def execute_inventory(observation: RailwayObservation) -> dict[str, Any]:
    keys = all_migratable_keys()
    return _report(
        PHASE_INVENTORY,
        ok=True,
        canonical_service=CANONICAL_SERVICE_NAME,
        legacy_source_service=LEGACY_SOURCE_SERVICE_NAME,
        canonical_presence=presence_only(observation.canonical.variables, keys),
        legacy_source_presence=presence_only(observation.legacy_source.variables, keys),
        canonical_deployment_id=observation.canonical.deployment_id or "absent",
        legacy_source_deployment_id=observation.legacy_source.deployment_id or "absent",
        canonical_domains=list(observation.canonical.domains),
        legacy_source_domains=list(observation.legacy_source.domains),
    )


def execute_dry_run_plan(
    observation: RailwayObservation,
    *,
    hmac_key: str,
) -> dict[str, Any]:
    plan = build_migration_plan(
        source_vars=observation.legacy_source.variables,
        dest_vars=observation.canonical.variables,
    )
    conflicts = detect_conflicts(
        source_vars=observation.legacy_source.variables,
        dest_vars=observation.canonical.variables,
        hmac_key=hmac_key,
    )
    merged_preview = dict(observation.canonical.variables)
    for key in plan["copy_from_source"]:
        merged_preview[key] = observation.legacy_source.variables.get(key, "")
    readiness = evaluate_meta_channel_readiness(
        merged_preview,
        routes=observation.canonical.routes,
        db_binding=observation.staging_db_wa_binding,
        acceptance_cutover_snapshot=observation.acceptance_cutover_snapshot,
        require_tenant_1_cutover=observation.require_tenant_1_acceptance_cutover,
    )
    gaps = list(readiness["channel_readiness_gaps"])
    d360_only = bool(readiness["d360_only_legacy_path"])
    ok = not conflicts and not gaps and not d360_only
    code = None
    if conflicts:
        code = CODE_CONFLICT_DETECTED
    elif d360_only:
        code = CODE_CHANNEL_D360_ONLY_LEGACY_PATH
    elif gaps:
        code = CODE_CHANNEL_READINESS_GAP
    return _report(
        PHASE_DRY_RUN_PLAN,
        ok=ok,
        code=code,
        plan=plan,
        conflict_count=len(conflicts),
        conflicts=conflicts,
        acceptance_target_path=readiness["acceptance_target_path"],
        meta_onboarding_target_path=readiness["meta_onboarding_target_path"],
        meta_onboarding_external_blocker=readiness["meta_onboarding_external_blocker"],
        tenant_1_acceptance_cutover_required=readiness["tenant_1_acceptance_cutover_required"],
        d360_only_legacy_path=d360_only,
        channel_readiness_gaps=gaps,
        channel_ready=bool(readiness["channel_ready"]) and not conflicts,
    )


def _fernet_from_key(raw_key: str) -> Any:
    from cryptography.fernet import Fernet

    digest = fingerprint_value(raw_key, key=SNAPSHOT_KEY_ENV, hmac_key="snapshot-key-derivation")
    key_bytes = base64.urlsafe_b64encode(digest.encode("utf-8")[:32].ljust(32, b"0"))
    return Fernet(key_bytes)


def build_snapshot_blob(
    observation: RailwayObservation,
    *,
    snapshot_key: str,
) -> dict[str, Any]:
    payload = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at_utc": _utc_now(),
        "project_id": observation.project_id,
        "environment_id": observation.environment_id,
        "services": {
            CANONICAL_SERVICE_NAME: {
                "service_id": observation.canonical.service_id,
                "variables": observation.canonical.variables,
                "deployment_id": observation.canonical.deployment_id,
                "source_revision": observation.canonical.source_revision,
                "domains": list(observation.canonical.domains),
                "routes": list(observation.canonical.routes),
            },
            LEGACY_SOURCE_SERVICE_NAME: {
                "service_id": observation.legacy_source.service_id,
                "variables": observation.legacy_source.variables,
                "deployment_id": observation.legacy_source.deployment_id,
                "source_revision": observation.legacy_source.source_revision,
                "domains": list(observation.legacy_source.domains),
                "routes": list(observation.legacy_source.routes),
            },
        },
    }
    token = _fernet_from_key(snapshot_key).encrypt(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).decode("utf-8")
    return {
        "snapshot_id": str(uuid.uuid4()),
        "encrypted_blob": token,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "service_ids": {
            "canonical": observation.canonical.service_id,
            "legacy_source": observation.legacy_source.service_id,
        },
    }


def restore_snapshot_blob(*, encrypted_blob: str, snapshot_key: str) -> dict[str, Any]:
    from cryptography.fernet import InvalidToken

    try:
        raw = _fernet_from_key(snapshot_key).decrypt(encrypted_blob.encode("utf-8"))
        payload = json.loads(raw.decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValueError(CODE_SNAPSHOT_INVALID) from exc
    if payload.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(CODE_SNAPSHOT_INVALID)
    return payload


def _validate_apply_variables(variables: Mapping[str, str]) -> str | None:
    for key in variables:
        if key in PROTECTED_VARIABLE_KEYS:
            return CODE_PROTECTED_VARIABLE_TOUCH
        if key in REFERENCE_BINDABLE_KEYS:
            value = (variables.get(key) or "").strip()
            if value and not is_reference_bindable(key, value):
                return CODE_REFERENCE_BINDING_REJECTED
        if key in SIGNATURE_MODE_KEYS:
            forbidden = FORBIDDEN_SIGNATURE_WEAKENING.get(key)
            if forbidden and (variables.get(key) or "").strip().lower() == forbidden:
                return CODE_SIGNATURE_WEAKENING
    return None


def build_apply_patch(
    observation: RailwayObservation,
    *,
    hmac_key: str,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    plan = build_migration_plan(
        source_vars=observation.legacy_source.variables,
        dest_vars=observation.canonical.variables,
    )
    conflicts = detect_conflicts(
        source_vars=observation.legacy_source.variables,
        dest_vars=observation.canonical.variables,
        hmac_key=hmac_key,
    )
    if conflicts:
        return {}, conflicts
    patch: dict[str, str] = {}
    for key in plan["copy_from_source"]:
        if key in MIGRATABLE_VARIABLE_KEYS:
            patch[key] = observation.legacy_source.variables[key]
        elif key in REFERENCE_BINDABLE_KEYS:
            value = observation.legacy_source.variables[key]
            if is_reference_bindable(key, value):
                patch[key] = value
    return patch, []


def execute_apply(
    observation: RailwayObservation,
    *,
    hmac_key: str,
    mutator: RailwayMutator,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    if not env_flag_enabled(env.get(MASTER_ENABLE_ENV)):
        return _report(PHASE_APPLY, ok=False, code=CODE_CONSOLIDATION_NOT_ENABLED)
    if (env.get(APPLY_CONFIRM_ENV) or "").strip() != APPLY_CONFIRM_TOKEN:
        return _report(PHASE_APPLY, ok=False, code=CODE_APPLY_NOT_CONFIRMED)

    shadow_block = gate_arch001_shadow_block(observation)
    if not shadow_block["ok"]:
        return _report(PHASE_APPLY, ok=False, code=CODE_ARCH001_SHADOW_ACTIVE)
    teardown = gate_arch001_teardown_proof(env)
    if not teardown["ok"]:
        return _report(PHASE_APPLY, ok=False, code=teardown.get("code"))

    patch, conflicts = build_apply_patch(observation, hmac_key=hmac_key)
    if conflicts:
        return _report(PHASE_APPLY, ok=False, code=CODE_CONFLICT_DETECTED, conflict_count=len(conflicts))
    reject = _validate_apply_variables(patch)
    if reject:
        return _report(PHASE_APPLY, ok=False, code=reject)

    # Keys only — never values in operator JSON output.
    mutator.set_service_variables(service_id=observation.canonical.service_id, variables=patch)
    deployment_id = mutator.trigger_deploy(service_id=observation.canonical.service_id)
    return _report(
        PHASE_APPLY,
        ok=True,
        canonical_service_id=observation.canonical.service_id,
        patched_keys=sorted(patch.keys()),
        deployment_id=deployment_id,
    )


def execute_verify_post_apply(
    *,
    pinned_revision: str,
    health_ok: bool,
    version_ok: bool,
    db_ok: bool,
    webhook_route_ok: bool,
    tenant_routing_ok: bool,
    signature_mode_ok: bool,
    accidental_flags: list[str],
    target_app_root: Path | None = None,
) -> dict[str, Any]:
    try:
        pin = normalize_revision_token(pinned_revision)
    except ValueError:
        return _report(PHASE_VERIFY, ok=False, code=CODE_RUNTIME_REVISION_MISMATCH)
    attestation = evaluate_runtime_revision_attestation(
        pinned_target_revision=pin,
        target_app_root=target_app_root,
    )
    checks = {
        "runtime_revision": attestation.ok,
        "health": health_ok,
        "version_endpoint": version_ok,
        "database": db_ok,
        "webhook_route": webhook_route_ok,
        "tenant_routing": tenant_routing_ok,
        "signature_mode": signature_mode_ok,
        "no_accidental_flags": len(accidental_flags) == 0,
    }
    ok = all(checks.values())
    code = None
    if not attestation.ok:
        code = CODE_RUNTIME_REVISION_MISMATCH
    elif not all(checks.values()):
        code = CODE_DEPLOY_ATTESTATION_FAILED
    return _report(
        PHASE_VERIFY,
        ok=ok,
        code=code,
        checks=checks,
        accidental_flags=accidental_flags,
        attested_revision=attestation.attested_revision,
        pinned_target_revision=pin,
    )


def execute_rollback(
    *,
    encrypted_blob: str,
    snapshot_key: str,
    mutator: RailwayMutator,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    if not env_flag_enabled(env.get(MASTER_ENABLE_ENV)):
        return _report(PHASE_ROLLBACK, ok=False, code=CODE_CONSOLIDATION_NOT_ENABLED)
    if (env.get(APPLY_CONFIRM_ENV) or "").strip() != APPLY_CONFIRM_TOKEN:
        return _report(PHASE_ROLLBACK, ok=False, code=CODE_APPLY_NOT_CONFIRMED)
    if not (snapshot_key or "").strip():
        return _report(PHASE_ROLLBACK, ok=False, code=CODE_SNAPSHOT_KEY_MISSING)
    try:
        snapshot = restore_snapshot_blob(encrypted_blob=encrypted_blob, snapshot_key=snapshot_key)
    except ValueError as exc:
        return _report(PHASE_ROLLBACK, ok=False, code=str(exc))

    canonical = snapshot["services"][CANONICAL_SERVICE_NAME]
    service_id = canonical["service_id"]
    variables = canonical["variables"]
    migratable = {k: v for k, v in variables.items() if k in all_migratable_keys()}
    reject = _validate_apply_variables(migratable)
    if reject:
        return _report(PHASE_ROLLBACK, ok=False, code=reject)

    mutator.set_service_variables(service_id=service_id, variables=migratable)
    deployment_id = mutator.trigger_deploy(service_id=service_id)
    return _report(
        PHASE_ROLLBACK,
        ok=True,
        canonical_service_id=service_id,
        restored_keys=sorted(migratable.keys()),
        deployment_id=deployment_id,
        snapshot_captured_at_utc=snapshot.get("captured_at_utc"),
    )


def routing_selection_guidance() -> dict[str, Any]:
    cutover = build_acceptance_cutover_guidance()
    return _report(
        PHASE_ROUTING_SELECTION,
        ok=True,
        canonical_public_app=CANONICAL_SERVICE_NAME,
        legacy_source_app=LEGACY_SOURCE_SERVICE_NAME,
        acceptance_target_path=ACCEPTANCE_TARGET_PROVIDER_PATH,
        meta_onboarding_target_path=META_ONBOARDING_TARGET_PATH,
        meta_onboarding_external_blocker=META_ONBOARDING_EXTERNAL_BLOCKER,
        meta_direct_webhook_route=META_DIRECT_WEBHOOK_ROUTE,
        tenant_1_acceptance_cutover=cutover,
        routing_policy=(
            "Route Meta Cloud API direct webhooks (/webhook/whatsapp) and BACKEND_URL "
            "to canonical nahla-saas only. Target onboarding is per-merchant Meta "
            "Embedded Signup (merchant-owned WABA, Phone Number ID, Access Token). "
            "360dialog is legacy/transition-only and must not satisfy acceptance "
            "readiness. Pre-verification acceptance may use Tenant 1 direct-Meta test "
            "channel cutover with explicit snapshot/rollback only."
        ),
        arch001_note="ARCH-001 shadow remains on nahla-saas until explicit teardown",
        auto_delete_services=False,
        auto_delete_domains=False,
    )


def execute_acceptance_cutover_guidance() -> dict[str, Any]:
    return _report(
        PHASE_ROUTING_SELECTION,
        ok=True,
        guidance=build_acceptance_cutover_guidance(),
    )


def execute_summary(observation: RailwayObservation, *, hmac_key: str) -> dict[str, Any]:
    dry_run = execute_dry_run_plan(observation, hmac_key=hmac_key)
    gaps = dry_run.get("channel_readiness_gaps") or []
    d360_only = bool(dry_run.get("d360_only_legacy_path"))
    execution_status = "BLOCK"
    if dry_run.get("ok"):
        execution_status = "READY_AFTER_ARCH001"
    elif dry_run.get("code") == CODE_CONFLICT_DETECTED:
        execution_status = "BLOCK_CONFLICT"
    elif d360_only:
        execution_status = "BLOCK_D360_ONLY_LEGACY_PATH"
    elif gaps:
        execution_status = "BLOCK_CREDENTIAL_GAP"
    return _report(
        PHASE_SUMMARY,
        ok=True,
        canonical_service=CANONICAL_SERVICE_NAME,
        legacy_source_service=LEGACY_SOURCE_SERVICE_NAME,
        execution_status=execution_status,
        acceptance_target_path=ACCEPTANCE_TARGET_PROVIDER_PATH,
        d360_only_legacy_path=d360_only,
        channel_readiness_gaps=gaps,
        conflict_count=dry_run.get("conflict_count", 0),
        recommendation=(
            "Use nahla-saas as canonical staging acceptance app on Meta Cloud API direct "
            "only. Target onboarding is per-merchant Meta Embedded Signup; Business "
            "Verification is the external blocker. Pre-verification acceptance may use "
            "Tenant 1 direct-Meta test channel cutover (acceptance-only, reversible). "
            "360dialog vars may remain for transition but never satisfy readiness. "
            "Do not disturb ARCH-001 shadow."
        ),
    )


class InMemoryRailwayClient:
    """Test double — never calls Railway API/CLI."""

    def __init__(self, observation: RailwayObservation) -> None:
        self.observation = observation
        self.patched: dict[str, dict[str, str]] = {}
        self.deploy_count = 0

    def observe_staging_services(self) -> RailwayObservation:
        return self.observation

    def set_service_variables(self, *, service_id: str, variables: Mapping[str, str]) -> None:
        self.patched[service_id] = dict(variables)

    def trigger_deploy(self, *, service_id: str) -> str:
        self.deploy_count += 1
        return f"deploy-{self.deploy_count}-{service_id[:8]}"


def _snapshot_dir() -> Path:
    raw = (os.environ.get(SNAPSHOT_DIR_ENV) or ".nahla-staging-acceptance-snapshots").strip()
    return Path(raw)


def _write_snapshot_file(blob: dict[str, Any]) -> Path:
    directory = _snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{blob['snapshot_id']}.json"
    public = {k: v for k, v in blob.items() if k != "encrypted_blob"}
    path.write_text(
        json.dumps({**public, "encrypted_blob": blob["encrypted_blob"]}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_fixture_observation(path: Path) -> RailwayObservation:
    payload = json.loads(path.read_text(encoding="utf-8"))

    def _service(raw: Mapping[str, Any], *, default_name: str, default_id: str) -> ServiceSnapshot:
        return ServiceSnapshot(
            service_id=str(raw.get("service_id") or default_id),
            service_name=str(raw.get("service_name") or default_name),
            variables={str(k): str(v) for k, v in dict(raw.get("variables") or {}).items()},
            deployment_id=str(raw.get("deployment_id") or ""),
            source_revision=str(raw.get("source_revision") or ""),
            domains=tuple(str(x) for x in raw.get("domains") or ()),
            routes=tuple(str(x) for x in raw.get("routes") or ()),
        )

    return RailwayObservation(
        project_id=str(payload.get("project_id") or STAGING_RAILWAY_PROJECT_ID),
        environment_id=str(payload.get("environment_id") or STAGING_RAILWAY_ENVIRONMENT_ID),
        canonical=_service(
            payload.get("canonical") or {},
            default_name=CANONICAL_SERVICE_NAME,
            default_id=CANONICAL_SERVICE_ID,
        ),
        legacy_source=_service(
            payload.get("legacy_source") or {},
            default_name=LEGACY_SOURCE_SERVICE_NAME,
            default_id=LEGACY_SOURCE_SERVICE_ID,
        ),
        staging_db_wa_binding=(
            {str(k): str(v) for k, v in dict(payload.get("staging_db_wa_binding") or {}).items()}
            if payload.get("staging_db_wa_binding")
            else None
        ),
        acceptance_cutover_snapshot=(
            dict(payload.get("acceptance_cutover_snapshot") or {})
            if payload.get("acceptance_cutover_snapshot")
            else None
        ),
        require_tenant_1_acceptance_cutover=bool(
            payload.get("require_tenant_1_acceptance_cutover")
        ),
    )


def _parse_fixture_args(args: list[str]) -> tuple[str, Path | None]:
    command = args[0]
    fixture_path: Path | None = None
    if len(args) >= 3 and args[1] == "--fixture":
        fixture_path = Path(args[2])
    return command, fixture_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    hmac_key = (os.environ.get(SNAPSHOT_KEY_ENV) or "ci-test-hmac-key").strip()

    if not args or args[0] in {"default-off", "default_off"}:
        result = execute_default_off_probe()
        _emit(result)
        return 0 if result["ok"] else 2

    if args[0] == "routing-selection":
        _emit(routing_selection_guidance())
        return 0

    if args[0] == "acceptance-cutover-guidance":
        _emit(execute_acceptance_cutover_guidance())
        return 0

    if args[0] == "preflight":
        identity = gate_staging_identity()
        _emit(identity)
        return 0 if identity["ok"] else 2

    command, fixture_path = _parse_fixture_args(args)
    if fixture_path is None:
        _emit(
            _report(
                PHASE_SUMMARY,
                ok=False,
                code=CODE_COMMAND_INVALID,
                message="fixture_required_for_railway_backed_commands",
            )
        )
        return 2

    observation = load_fixture_observation(fixture_path)
    allowlist = gate_railway_allowlist(observation)
    if not allowlist["ok"]:
        _emit(allowlist)
        return 2

    if command == "inventory":
        _emit(execute_inventory(observation))
        return 0

    if command == "dry-run-plan":
        result = execute_dry_run_plan(observation, hmac_key=hmac_key)
        _emit(result)
        return 0 if result.get("code") != CODE_CONFLICT_DETECTED else 2

    if command == "summary":
        _emit(execute_summary(observation, hmac_key=hmac_key))
        return 0

    if command == "snapshot":
        snapshot_key = (os.environ.get(SNAPSHOT_KEY_ENV) or "").strip()
        if not snapshot_key:
            _emit(_report(PHASE_SNAPSHOT, ok=False, code=CODE_SNAPSHOT_KEY_MISSING))
            return 2
        blob = build_snapshot_blob(observation, snapshot_key=snapshot_key)
        path = _write_snapshot_file(blob)
        _emit(
            _report(
                PHASE_SNAPSHOT,
                ok=True,
                snapshot_id=blob["snapshot_id"],
                snapshot_path=str(path),
                service_ids=blob["service_ids"],
            )
        )
        return 0

    _emit(
        _report(
            PHASE_SUMMARY,
            ok=False,
            code=CODE_COMMAND_INVALID,
            message="unknown_fixture_command",
            command=command,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
