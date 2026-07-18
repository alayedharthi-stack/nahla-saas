"""Post-shadow real-channel conversational acceptance operator (default-off).

Preparation and gated execution for Tenant 1 intensive then Tenant 33 limited
acceptance. Does not activate tenants or send channel messages unless all
fail-closed gates pass and explicit operator confirmation env vars are set.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
    normalize_revision_token,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (
    ALLOWLIST_PHONES_ENV,
    ARCH001_SHADOW_SIGNOFF_ENV,
    CHANNEL_PREFLIGHT_ENV_NAMES,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_COMMAND_INVALID,
    CODE_EXECUTION_NOT_CONFIRMED,
    CODE_MANIFEST_INVALID,
    CODE_PHONE_NOT_ALLOWLISTED,
    CODE_PROBE_FAILED,
    CODE_PROVIDER_SANDBOX_UNAVAILABLE,
    CODE_REAL_CHANNEL_REQUIRED,
    CODE_RUNTIME_REVISION_MISMATCH,
    CODE_STAGING_IDENTITY_REJECTED,
    CODE_STORE_AI_MODE_INVALID,
    CODE_TENANT_1_NOT_PASSED,
    CODE_TENANT_NOT_ALLOWED,
    DEFECT_BUNDLE_DIR,
    EVIDENCE_ACCUMULATION_DIR,
    EVIDENCE_SCHEMA_VERSION,
    EXECUTION_CONFIRM_ENV,
    EXECUTION_PATH_DIRECT_CODE_PROBE,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MASTER_ENABLE_ENV,
    PHASE_ARCH001_SHADOW_SIGNOFF_GATE,
    PHASE_CHANNEL_HEALTH,
    PHASE_CONFIG_SNAPSHOT,
    PHASE_DEFAULT_OFF,
    PHASE_DEFECT_BUNDLE,
    PHASE_READINESS_PREFLIGHT,
    PHASE_RUNTIME_REVISION_ATTESTATION,
    PHASE_SUMMARY,
    PHASE_TEARDOWN,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    PINNED_REVISION_ENV,
    PROVENANCE_FIELDS,
    REPORT_SCHEMA_VERSION,
    STAGING_ENVIRONMENT_ENV,
    STAGING_ENVIRONMENT_VALUE,
    STAGING_IDENTITY_CLASS,
    STAGING_PROJECT_ENV,
    STAGING_PROJECT_VALUE,
    TENANT_1_INTENSIVE,
    TENANT_1_PASS_CONFIRM_ENV,
    TENANT_1_PHONE_ENV,
    TENANT_33_LIMITED,
    TENANT_33_PHONE_ENV,
    count_scenarios_by_phase,
    env_flag_enabled,
    hash_identifier,
    load_scenario_manifest,
    mask_phone_tail,
    parse_allowlist_phones,
    required_config_snapshot_keys,
)
from scripts.operators.staging_migration_operator_gates import validate_staging_identity


def resolve_app_root(artifact_root: Path | None = None) -> Path:
    root = (artifact_root or Path(__file__).resolve().parents[2]).resolve()
    if (root / "backend").is_dir():
        return root
    raise ValueError("artifact_root_invalid")


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        **payload,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _credential_presence(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = env or os.environ
    return {name: "present" if (env.get(name) or "").strip() else "absent" for name in CHANNEL_PREFLIGHT_ENV_NAMES}


def execute_default_off_probe() -> dict[str, Any]:
    enabled = env_flag_enabled(os.environ.get(MASTER_ENABLE_ENV))
    return _report(
        PHASE_DEFAULT_OFF,
        ok=not enabled,
        acceptance_enabled=enabled,
        message=(
            "acceptance_disabled_by_default"
            if not enabled
            else "acceptance_enabled_requires_full_gate_chain"
        ),
    )


def gate_staging_identity(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    failure = validate_staging_identity(
        env,
        staging_project_env=STAGING_PROJECT_ENV,
        staging_environment_env=STAGING_ENVIRONMENT_ENV,
        staging_project_value=STAGING_PROJECT_VALUE,
        staging_environment_value=STAGING_ENVIRONMENT_VALUE,
    )
    if failure is not None:
        return _report(
            PHASE_READINESS_PREFLIGHT,
            ok=False,
            code=CODE_STAGING_IDENTITY_REJECTED,
            staging_identity_class=STAGING_IDENTITY_CLASS,
            error_class=failure.error_class,
            stage=failure.stage,
        )
    return _report(
        PHASE_READINESS_PREFLIGHT,
        ok=True,
        staging_identity_class=STAGING_IDENTITY_CLASS,
        credential_presence=_credential_presence(env),
    )


def gate_arch001_shadow_signoff() -> dict[str, Any]:
    confirmed = env_flag_enabled(os.environ.get(ARCH001_SHADOW_SIGNOFF_ENV))
    return _report(
        PHASE_ARCH001_SHADOW_SIGNOFF_GATE,
        ok=confirmed,
        code=None if confirmed else CODE_ARCH001_SIGNOFF_MISSING,
        arch001_shadow_signoff_confirmed=confirmed,
        note="Set after ARCH-001 48h synthetic shadow signoff artifact is approved",
    )


def gate_runtime_revision_attestation(
    *,
    pinned_target_revision: str | None = None,
    target_app_root: Path | None = None,
) -> dict[str, Any]:
    pin_raw = pinned_target_revision or os.environ.get(PINNED_REVISION_ENV)
    if not pin_raw:
        return _report(
            PHASE_RUNTIME_REVISION_ATTESTATION,
            ok=False,
            code=CODE_RUNTIME_REVISION_MISMATCH,
            blockers=["pinned_revision_missing"],
        )
    try:
        pin = normalize_revision_token(pin_raw)
    except ValueError:
        return _report(
            PHASE_RUNTIME_REVISION_ATTESTATION,
            ok=False,
            code=CODE_RUNTIME_REVISION_MISMATCH,
        )
    attestation = evaluate_runtime_revision_attestation(
        pinned_target_revision=pin,
        target_app_root=target_app_root,
    )
    payload = attestation.to_dict()
    for key in ("code", "ok"):
        payload.pop(key, None)
    return _report(
        PHASE_RUNTIME_REVISION_ATTESTATION,
        ok=attestation.ok,
        code=attestation.code,
        **payload,
    )


def _resolve_test_phones() -> dict[str, Any]:
    allowlist = parse_allowlist_phones(os.environ.get(ALLOWLIST_PHONES_ENV))
    t1 = re.sub(r"\D", "", os.environ.get(TENANT_1_PHONE_ENV, ""))
    t33 = re.sub(r"\D", "", os.environ.get(TENANT_33_PHONE_ENV, ""))
    return {
        "allowlist_count": len(allowlist),
        "allowlist_hashes": [hash_identifier(p) for p in allowlist],
        "tenant_1_phone_hash": hash_identifier(t1) if t1 else None,
        "tenant_33_phone_hash": hash_identifier(t33) if t33 else None,
        "tenant_1_phone_masked": mask_phone_tail(t1),
        "tenant_33_phone_masked": mask_phone_tail(t33),
    }


def build_config_snapshot(
    *,
    tenant_id: int,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized config snapshot template (no secrets, no raw phones)."""
    phones = _resolve_test_phones()
    return {
        key: None
        for key in required_config_snapshot_keys()
    } | {
        "tenant_id": tenant_id,
        "store_ai_mode": "test",
        "store_ai_enabled": True,
        "ai_test_allowed_numbers_hash": phones.get(
            "tenant_1_phone_hash" if tenant_id == TENANT_1_INTENSIVE else "tenant_33_phone_hash"
        ),
        "ai_paused": False,
        "handoff_active": False,
        "subscription_status": "active",
        "blocklist_hash": None,
        "pinned_revision": os.environ.get(PINNED_REVISION_ENV),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "captured_at_utc": _utc_now(),
    }


def execute_config_snapshot_preflight(*, tenant_id: int) -> dict[str, Any]:
    if tenant_id not in {TENANT_1_INTENSIVE, TENANT_33_LIMITED}:
        return _report(
            PHASE_CONFIG_SNAPSHOT,
            ok=False,
            code=CODE_TENANT_NOT_ALLOWED,
            tenant_id=tenant_id,
        )
    snapshot = build_config_snapshot(tenant_id=tenant_id)
    return _report(
        PHASE_CONFIG_SNAPSHOT,
        ok=True,
        tenant_id=tenant_id,
        snapshot=snapshot,
        restoration_note="Restore tenant ai_settings from snapshot file after session",
    )


def execute_channel_health_preflight(*, tenant_id: int) -> dict[str, Any]:
    """Read-only channel health gate. Blocks if provider credentials absent."""
    creds = _credential_presence()
    required_for_health = ("DATABASE_URL", "BACKEND_URL")
    missing = [name for name in required_for_health if creds.get(name) == "absent"]
    if missing:
        return _report(
            PHASE_CHANNEL_HEALTH,
            ok=False,
            code=CODE_CHANNEL_HEALTH_BLOCKED,
            tenant_id=tenant_id,
            missing_credentials=missing,
            credential_presence=creds,
            probe_command=f"python scripts/probe_d360_forwarding.py --tenant {tenant_id}",
        )

    d360_ready = creds.get("D360_API_BASE_URL") == "present"
    meta_ready = creds.get("META_APP_SECRET") == "present"
    if not d360_ready and not meta_ready:
        return _report(
            PHASE_CHANNEL_HEALTH,
            ok=False,
            code=CODE_PROVIDER_SANDBOX_UNAVAILABLE,
            tenant_id=tenant_id,
            credential_presence=creds,
            block_reason="no_whatsapp_provider_credentials",
            note="BLOCK: cannot claim real-channel E2E without D360 or Meta credentials",
        )

    return _report(
        PHASE_CHANNEL_HEALTH,
        ok=True,
        tenant_id=tenant_id,
        credential_presence=creds,
        provider_paths_available={
            "d360": d360_ready,
            "meta": meta_ready,
        },
        probe_command=f"python scripts/probe_d360_forwarding.py --tenant {tenant_id}",
        execution_path_required=EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    )


def _execution_gates(*, phase: str, tenant_id: int) -> dict[str, Any] | None:
    if not env_flag_enabled(os.environ.get(MASTER_ENABLE_ENV)):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_ACCEPTANCE_NOT_ENABLED)
    if not env_flag_enabled(os.environ.get(EXECUTION_CONFIRM_ENV)):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_EXECUTION_NOT_CONFIRMED)
    if not env_flag_enabled(os.environ.get(ARCH001_SHADOW_SIGNOFF_ENV)):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_ARCH001_SIGNOFF_MISSING)
    if phase == PHASE_TENANT_33_LIMITED and not env_flag_enabled(
        os.environ.get(TENANT_1_PASS_CONFIRM_ENV)
    ):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_TENANT_1_NOT_PASSED)
    if tenant_id not in {TENANT_1_INTENSIVE, TENANT_33_LIMITED}:
        return _report(PHASE_SUMMARY, ok=False, code=CODE_TENANT_NOT_ALLOWED)
    return None


def execute_scenario_plan(
    *,
    phase: str,
    app_root: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """List scenarios for a phase. Execution is blocked unless dry_run=False and gates pass."""
    blocked = _execution_gates(phase=phase, tenant_id=TENANT_1_INTENSIVE if phase == PHASE_TENANT_1_INTENSIVE else TENANT_33_LIMITED)
    if blocked and not dry_run:
        return blocked

    try:
        manifest = load_scenario_manifest(app_root)
    except ValueError as exc:
        return _report(phase, ok=False, code=str(exc) or CODE_MANIFEST_INVALID)

    scenarios = [
        row
        for row in manifest.get("scenarios", [])
        if str(row.get("phase")) == phase
    ]
    plan_rows = [
        {
            "scenario_id": row["scenario_id"],
            "taxonomy": row["taxonomy"],
            "execution_path": row["execution_path"],
            "automation_class": row["automation_class"],
            "latency_budget_ms": row["latency_budget_ms"],
        }
        for row in scenarios
    ]
    return _report(
        phase,
        ok=True,
        dry_run=dry_run,
        scenario_count=len(plan_rows),
        scenarios=plan_rows,
        execution_blocked=bool(blocked),
        note=(
            "plan_only_no_messages_sent"
            if dry_run
            else "execution_requires_real_channel_webhook_path"
        ),
    )


def build_defect_bundle(
    *,
    scenario_id: str,
    failure_class: str,
    correlation_id: str,
    sanitized_evidence: Mapping[str, Any],
    app_root: Path | None = None,
) -> dict[str, Any]:
    root = resolve_app_root(app_root)
    bundle_dir = root / DEFECT_BUNDLE_DIR
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = f"{scenario_id}-{correlation_id[:8]}"
    bundle_path = bundle_dir / f"{bundle_id}.json"
    payload = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "scenario_id": scenario_id,
        "failure_class": failure_class,
        "correlation_id": correlation_id,
        "created_at_utc": _utc_now(),
        "classification": {
            "severity": "p0" if "prohibited" in failure_class else "p1",
            "routing": "eval_regression_engineering",
            "auto_merge_fixes": False,
        },
        "sanitized_evidence": dict(sanitized_evidence),
        "provenance_chain_required": list(PROVENANCE_FIELDS),
        "defect_workflow": [
            "file_issue_with_bundle_id",
            "map_to_eval_regression_mapping_in_manifest",
            "implement_fix_via_normal_ci",
            "constitution_compliance_required",
            "no_auto_merge_during_acceptance_window",
        ],
    }
    bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _report(
        PHASE_DEFECT_BUNDLE,
        ok=True,
        bundle_id=bundle_id,
        bundle_path=str(bundle_path.relative_to(root)),
    )


def build_evidence_record(
    *,
    scenario_id: str,
    correlation_id: str,
    execution_path: str,
    pass_fail: str,
    provenance: Mapping[str, Any],
    state_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "correlation_id": correlation_id,
        "execution_path": execution_path,
        "execution_path_label": (
            "real_channel_webhook"
            if execution_path == EXECUTION_PATH_REAL_CHANNEL_WEBHOOK
            else "direct_code_probe_NOT_REAL_CHANNEL"
        ),
        "recorded_at_utc": _utc_now(),
        "pass_fail": pass_fail,
        "provenance_chain": {key: provenance.get(key) for key in PROVENANCE_FIELDS},
        "state_evidence": dict(state_evidence),
        "customer_text_excerpt_hash": hash_identifier(str(state_evidence.get("body_excerpt") or "")),
    }


def teardown_instructions() -> dict[str, Any]:
    return _report(
        PHASE_TEARDOWN,
        ok=True,
        steps=[
            f"unset {MASTER_ENABLE_ENV}",
            f"unset {EXECUTION_CONFIRM_ENV}",
            f"unset {ARCH001_SHADOW_SIGNOFF_ENV}",
            f"unset {TENANT_1_PASS_CONFIRM_ENV}",
            "restore tenant ai_settings from config snapshot",
            "verify store_ai_mode=test and allowlist unchanged or reverted per runbook",
            "archive evidence to docs/engineering/staging-evidence/",
            "confirm no outbound messages to non-allowlisted numbers",
        ],
        arch001_shadow_teardown=(
            "python -m scripts.operators.product_availability_truth_guard_shadow_observation teardown"
        ),
    )


def execute_readiness_preflight(*, app_root: Path | None = None) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    phases.append(execute_default_off_probe())
    phases.append(gate_staging_identity())
    phases.append(gate_arch001_shadow_signoff())

    try:
        manifest = load_scenario_manifest(app_root)
        counts = count_scenarios_by_phase(manifest)
        manifest_ok = True
    except ValueError as exc:
        manifest_ok = False
        counts = {"error": str(exc)}

    phases.append(
        _report(
            "manifest_validation",
            ok=manifest_ok,
            scenario_counts=counts,
        )
    )
    phases.append(execute_channel_health_preflight(tenant_id=TENANT_1_INTENSIVE))
    phases.append(execute_config_snapshot_preflight(tenant_id=TENANT_1_INTENSIVE))

    ok = all(
        bool(p.get("ok"))
        for p in phases
        if p.get("phase")
        not in {PHASE_ARCH001_SHADOW_SIGNOFF_GATE}
    ) and manifest_ok
    return _report(
        PHASE_SUMMARY,
        ok=ok,
        phases=phases,
        evidence_accumulation_path=str(EVIDENCE_ACCUMULATION_DIR),
        defect_bundle_path=str(DEFECT_BUNDLE_DIR),
        go_block_recommendation="BLOCK" if not ok else "BLOCK_UNTIL_ARCH001_ENDS",
    )


def execute_full_preflight(
    *,
    app_root: Path | None = None,
    pinned_target_revision: str | None = None,
) -> dict[str, Any]:
    readiness = execute_readiness_preflight(app_root=app_root)
    phases = list(readiness.get("phases", []))
    if pinned_target_revision or os.environ.get(PINNED_REVISION_ENV):
        phases.append(
            gate_runtime_revision_attestation(
                pinned_target_revision=pinned_target_revision,
                target_app_root=app_root,
            )
        )
    ok = readiness.get("ok", False)
    for phase in phases:
        if phase.get("phase") == PHASE_RUNTIME_REVISION_ATTESTATION:
            ok = ok and bool(phase.get("ok"))
    return _report(
        PHASE_SUMMARY,
        ok=ok,
        phases=phases,
        tenant_1_plan=execute_scenario_plan(
            phase=PHASE_TENANT_1_INTENSIVE, app_root=app_root, dry_run=True
        ),
        tenant_33_plan=execute_scenario_plan(
            phase=PHASE_TENANT_33_LIMITED, app_root=app_root, dry_run=True
        ),
        teardown=teardown_instructions(),
        go_block_recommendation="BLOCK_UNTIL_ARCH001_ENDS_AND_SIGNOFF",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["default-off"]:
            _emit(execute_default_off_probe())
            return 0
        if arguments == ["preflight"]:
            _emit(execute_readiness_preflight())
            return 0
        if arguments[:1] == ["full-preflight"]:
            pin = arguments[1] if len(arguments) > 1 else None
            _emit(execute_full_preflight(pinned_target_revision=pin))
            return 0
        if arguments == ["manifest-validate"]:
            manifest = load_scenario_manifest()
            _emit(
                _report(
                    "manifest_validation",
                    ok=True,
                    scenario_counts=count_scenarios_by_phase(manifest),
                )
            )
            return 0
        if arguments == ["tenant-1-plan"]:
            _emit(
                execute_scenario_plan(phase=PHASE_TENANT_1_INTENSIVE, dry_run=True)
            )
            return 0
        if arguments == ["tenant-33-plan"]:
            _emit(
                execute_scenario_plan(phase=PHASE_TENANT_33_LIMITED, dry_run=True)
            )
            return 0
        if arguments == ["teardown"]:
            _emit(teardown_instructions())
            return 0
        if arguments[:2] == ["defect-bundle", "template"]:
            scenario_id = arguments[2] if len(arguments) > 2 else "unknown_scenario"
            corr = str(uuid.uuid4())
            _emit(
                build_defect_bundle(
                    scenario_id=scenario_id,
                    failure_class="template_probe",
                    correlation_id=corr,
                    sanitized_evidence={"probe": True},
                )
            )
            return 0
        raise ValueError(CODE_COMMAND_INVALID)
    except ValueError:
        _emit(_report(PHASE_SUMMARY, ok=False, code=CODE_COMMAND_INVALID))
        return 2
    except BaseException:
        _emit(_report(PHASE_SUMMARY, ok=False, code=CODE_PROBE_FAILED))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
