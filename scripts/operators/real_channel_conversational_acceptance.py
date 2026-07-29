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

from sqlalchemy import create_engine, text

from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
    normalize_revision_token,
)
from scripts.operators.meta_acceptance_channel_evidence_contract import (
    CODE_DB_WA_BINDING_MISSING,
    CODE_DB_WA_BINDING_MISMATCH,
    EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
    WEBHOOK_ATTESTATION_ARTIFACT_ENV,
    WEBHOOK_ATTESTATION_HMAC_KEY_ENV,
    evaluate_meta_config_present,
    evaluate_operator_attested_channel_ready,
    load_webhook_attestation_artifact,
)
from scripts.operators.product_availability_preprod_synthetic_signoff_v2 import (
    verify_arch001_preprod_signoff_for_gate,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (
    ALLOWLIST_PHONES_ENV,
    ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV,
    ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV,
    CHANNEL_PREFLIGHT_ENV_NAMES,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_AI_TEST_ALLOWLIST_INVALID,
    CODE_APPROVED_EGRESS_CONFIGURATION_MISSING,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_D360_ONLY_LEGACY_PATH,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_CHANNEL_READINESS_GAP,
    CODE_COMMAND_INVALID,
    CODE_DATABASE_BINDING_REJECTED,
    CODE_DATABASE_QUERY_FAILED,
    CODE_DB_WA_BINDING_INVALID,
    CODE_DB_WA_BINDING_MISSING,
    CODE_DB_WA_BINDING_MISMATCH,
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
    CODE_TENANT_SETTINGS_MISSING,
    CODE_ROLLBACK_SNAPSHOT_INVALID,
    CODE_ROLLBACK_SNAPSHOT_MISSING,
    CODE_ROLLBACK_SNAPSHOT_STALE,
    CODE_WEBHOOK_ATTESTATION_FORGED,
    CODE_WEBHOOK_ATTESTATION_MISSING,
    CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH,
    CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED,
    CODE_WEBHOOK_ATTESTATION_STALE,
    CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH,
    CODE_WEBHOOK_OBSERVATION_INVALID,
    CODE_WEBHOOK_OBSERVATION_STALE,
    DEFECT_BUNDLE_DIR,
    D360_LEGACY_OBSERVABILITY_ENV_NAMES,
    EVIDENCE_ACCUMULATION_DIR,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
    EVIDENCE_CHANNEL_DIRECT_CODE_PROBE,
    EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
    EXECUTION_CONFIRM_ENV,
    EXECUTION_PATH_DIRECT_CODE_PROBE,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MASTER_ENABLE_ENV,
    META_DIRECT_WEBHOOK_ROUTE,
    META_ONBOARDING_EXTERNAL_BLOCKER,
    META_ONBOARDING_TARGET_PATH,
    META_READINESS_REQUIRED_ENV_NAMES,
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
    WEBHOOK_ATTESTATION_ARTIFACT_ENV,
    WEBHOOK_ATTESTATION_HMAC_KEY_ENV,
    config_snapshot_db_io_permitted,
    count_scenarios_by_phase,
    env_flag_enabled,
    evaluate_execution_gate_chain,
    hash_identifier,
    load_scenario_manifest,
    mask_phone_tail,
    parse_allowlist_phones,
    read_only_preflight_db_io_permitted,
    required_config_snapshot_keys,
)
from scripts.operators.staging_migration_operator_gates import validate_staging_identity

DEPLOYMENT_ID_ENV = "RAILWAY_DEPLOYMENT_ID"
DATABASE_URL_ENV = "DATABASE_URL"


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
    verified = verify_arch001_preprod_signoff_for_gate()
    ok = verified.get("ok") is True
    return _report(
        PHASE_ARCH001_SHADOW_SIGNOFF_GATE,
        ok=ok,
        code=None if ok else (verified.get("code") or CODE_ARCH001_SIGNOFF_MISSING),
        arch001_preprod_signoff_v2_valid=ok,
        blockers=verified.get("blockers") or [],
        note=(
            "Requires HMAC-signed production product_availability_preprod_synthetic_signoff_v2 "
            "artifact bound to current pinned revision, manifest digest, and isolated service identity"
        ),
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


_PHONE_ENV_BY_TENANT = {
    TENANT_1_INTENSIVE: TENANT_1_PHONE_ENV,
    TENANT_33_LIMITED: TENANT_33_PHONE_ENV,
}


def _phone_env_for_tenant(tenant_id: int) -> str:
    try:
        return _PHONE_ENV_BY_TENANT[tenant_id]
    except KeyError as exc:
        raise ValueError(CODE_TENANT_NOT_ALLOWED) from exc


def _read_tenant_ai_settings(
    tenant_id: int,
    *,
    conn: Any | None = None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Read-only tenant_settings.ai_settings; no writes."""
    if conn is not None:
        ai_settings = conn.execute(
            text("SELECT ai_settings FROM tenant_settings WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one_or_none()
        if not isinstance(ai_settings, Mapping):
            return None, CODE_TENANT_SETTINGS_MISSING
        return dict(ai_settings), None

    database_url = (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        return None, CODE_DATABASE_BINDING_REJECTED
    try:
        engine = create_engine(database_url)
        with engine.connect() as connection:
            return _read_tenant_ai_settings(tenant_id, conn=connection)
    except Exception:  # noqa: silent-ok — read-only preflight; fail closed
        return None, CODE_DATABASE_QUERY_FAILED


def _validate_tenant_ai_preflight(
    *,
    tenant_id: int,
    ai_settings: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> str | None:
    env_map = env or os.environ
    if str(ai_settings.get("store_ai_mode") or "") != "test":
        return CODE_STORE_AI_MODE_INVALID
    if ai_settings.get("store_ai_enabled", True) is not True:
        return CODE_STORE_AI_MODE_INVALID

    raw_allowlist = ai_settings.get("ai_test_allowed_numbers")
    if not isinstance(raw_allowlist, list) or not raw_allowlist:
        return CODE_AI_TEST_ALLOWLIST_INVALID

    db_allowlist = {
        re.sub(r"\D", "", str(value))
        for value in raw_allowlist
        if re.sub(r"\D", "", str(value))
    }
    if not db_allowlist:
        return CODE_AI_TEST_ALLOWLIST_INVALID

    env_allowlist = set(parse_allowlist_phones(env_map.get(ALLOWLIST_PHONES_ENV)))
    if not env_allowlist:
        return CODE_PHONE_NOT_ALLOWLISTED

    phone = re.sub(r"\D", "", str(env_map.get(_phone_env_for_tenant(tenant_id), "")))
    if not phone or phone not in env_allowlist:
        return CODE_PHONE_NOT_ALLOWLISTED
    if phone not in db_allowlist or not db_allowlist.issubset(env_allowlist):
        return CODE_PHONE_NOT_ALLOWLISTED
    return None


def build_config_snapshot(
    *,
    tenant_id: int,
    ai_settings: Mapping[str, Any],
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build a sanitized snapshot from actual tenant settings (no fabrication)."""
    allowlist_hashes = [
        hash_identifier(re.sub(r"\D", "", str(phone)))
        for phone in (ai_settings.get("ai_test_allowed_numbers") or [])
    ]
    return {
        key: None
        for key in required_config_snapshot_keys()
    } | {
        "tenant_id": tenant_id,
        "store_ai_mode": str(ai_settings.get("store_ai_mode") or ""),
        "store_ai_enabled": ai_settings.get("store_ai_enabled"),
        "ai_test_allowed_numbers_hash": allowlist_hashes[0] if len(allowlist_hashes) == 1 else None,
        "ai_test_allowed_numbers_hashes": allowlist_hashes,
        "ai_paused": ai_settings.get("ai_paused"),
        "handoff_active": ai_settings.get("handoff_active"),
        "subscription_status": None,
        "blocklist_hash": None,
        "pinned_revision": os.environ.get(PINNED_REVISION_ENV),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "captured_at_utc": _utc_now(),
        "source": "tenant_settings_read_only",
    }


def execute_config_snapshot_preflight(
    *,
    tenant_id: int,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env_map = dict(env or os.environ)
    if tenant_id not in {TENANT_1_INTENSIVE, TENANT_33_LIMITED}:
        return _report(
            PHASE_CONFIG_SNAPSHOT,
            ok=False,
            code=CODE_TENANT_NOT_ALLOWED,
            tenant_id=tenant_id,
            db_io_performed=False,
        )

    permitted, gate_code = config_snapshot_db_io_permitted(env_map)
    if not permitted:
        return _report(
            PHASE_CONFIG_SNAPSHOT,
            ok=False,
            code=gate_code,
            tenant_id=tenant_id,
            db_io_performed=False,
            note="read_only_db_skipped_until_acceptance_gates_pass",
        )

    ai_settings, read_error = _read_tenant_ai_settings(tenant_id)
    if read_error:
        return _report(
            PHASE_CONFIG_SNAPSHOT,
            ok=False,
            code=read_error,
            tenant_id=tenant_id,
            db_io_performed=True,
        )

    validation_error = _validate_tenant_ai_preflight(
        tenant_id=tenant_id,
        ai_settings=ai_settings or {},
        env=env_map,
    )
    if validation_error:
        return _report(
            PHASE_CONFIG_SNAPSHOT,
            ok=False,
            code=validation_error,
            tenant_id=tenant_id,
            db_io_performed=True,
            snapshot=build_config_snapshot(
                tenant_id=tenant_id,
                ai_settings=ai_settings or {},
            ),
        )

    snapshot = build_config_snapshot(tenant_id=tenant_id, ai_settings=ai_settings or {})
    return _report(
        PHASE_CONFIG_SNAPSHOT,
        ok=True,
        tenant_id=tenant_id,
        db_io_performed=True,
        snapshot=snapshot,
        restoration_note="Restore tenant ai_settings from snapshot file after session",
    )


def _load_whatsapp_connection_row(tenant_id: int) -> dict[str, Any] | None:
    database_url = (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+")):
        return None
    try:
        engine = create_engine(database_url)
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT tenant_id, provider, status, sending_enabled, phone_number_id, "
                    "whatsapp_business_account_id "
                    "FROM whatsapp_connections WHERE tenant_id = :tenant_id "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"tenant_id": tenant_id},
            ).mappings().first()
        return dict(row) if row else None
    except Exception:  # noqa: silent-ok — read-only preflight; DB unavailable fails closed via missing binding
        return None


def _resolve_webhook_attestation_artifact(
    attestation_artifact: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if attestation_artifact is not None:
        return dict(attestation_artifact)
    artifact_path = (os.environ.get(WEBHOOK_ATTESTATION_ARTIFACT_ENV) or "").strip()
    if not artifact_path:
        return None
    return load_webhook_attestation_artifact(artifact_path)


def _failure_code_for_channel_evidence(evidence: Mapping[str, Any]) -> str:
    if "d360_only_legacy_path" in (evidence.get("channel_evidence_gaps") or []):
        return CODE_CHANNEL_D360_ONLY_LEGACY_PATH
    attestation_gaps = list(evidence.get("webhook_attestation_gaps") or [])
    if "webhook_attestation_artifact" in attestation_gaps:
        return CODE_WEBHOOK_ATTESTATION_MISSING
    if "webhook_attestation_signature" in attestation_gaps:
        return CODE_WEBHOOK_ATTESTATION_FORGED
    if any(
        gap in attestation_gaps
        for gap in (
            "webhook_attestation.expired",
            "webhook_attestation.not_yet_valid",
            "webhook_attestation.validity_window",
            "webhook_attestation.validity_window_too_long",
        )
    ):
        return CODE_WEBHOOK_ATTESTATION_STALE
    if "webhook_attestation.pinned_revision" in attestation_gaps:
        return CODE_WEBHOOK_ATTESTATION_REVISION_MISMATCH
    if "webhook_attestation.tenant_id" in attestation_gaps:
        return CODE_WEBHOOK_ATTESTATION_TENANT_MISMATCH
    if "webhook_attestation.observed_callback_route" in attestation_gaps:
        return CODE_WEBHOOK_ATTESTATION_ROUTE_UNOBSERVED
    if "webhook_attestation.backend_url_fingerprint" in attestation_gaps:
        return CODE_CHANNEL_READINESS_GAP
    if any(
        gap in attestation_gaps
        for gap in (
            "webhook_attestation.observation_source",
            "webhook_attestation.observer_id",
            "webhook_attestation.observation_evidence_digest",
            "webhook_attestation.observation_after_issued",
        )
    ):
        return CODE_WEBHOOK_OBSERVATION_INVALID
    if "webhook_attestation.observation_stale" in attestation_gaps:
        return CODE_WEBHOOK_OBSERVATION_STALE
    if any(gap.startswith("rollback_snapshot_evidence") for gap in attestation_gaps):
        if any(
            gap in attestation_gaps
            for gap in (
                "rollback_snapshot_evidence",
                "rollback_snapshot_evidence.snapshot_fingerprint",
            )
        ):
            return CODE_ROLLBACK_SNAPSHOT_MISSING
        if "rollback_snapshot_evidence.captured_after_observation" in attestation_gaps:
            return CODE_ROLLBACK_SNAPSHOT_STALE
        return CODE_ROLLBACK_SNAPSHOT_INVALID
    binding_gaps = list(evidence.get("db_wa_binding_gaps") or [])
    if binding_gaps:
        if "db_wa_binding.row_missing" in binding_gaps:
            return CODE_DB_WA_BINDING_MISSING
        if "db_wa_binding.d360_provider_rejected" in binding_gaps:
            return CODE_DB_WA_BINDING_INVALID
        if any("fingerprint" in gap for gap in binding_gaps):
            return CODE_DB_WA_BINDING_MISMATCH
        return CODE_DB_WA_BINDING_MISSING
    return CODE_CHANNEL_READINESS_GAP


_UNSET_DB_ROW = object()


def execute_channel_health_preflight(
    *,
    tenant_id: int,
    attestation_artifact: Mapping[str, Any] | None = None,
    db_row: Mapping[str, Any] | None | object = _UNSET_DB_ROW,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read-only Meta-direct channel gate.

    ``meta_config_present`` reflects env-key presence only.
    ``operator_attested_channel_ready`` requires signed operator webhook
    observation attestation plus tenant-specific read-only DB binding evidence.
    This is not post-send ``actual_provider_channel`` proof.

    DB reads occur only when master-enable, staging identity, and DB binding
    all permit read-only preflight I/O.
    """
    env_map = dict(env or os.environ)
    db_io_permitted, db_gate_code = read_only_preflight_db_io_permitted(env_map)
    creds = _credential_presence(env_map)
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
            meta_config_present=False,
            operator_attested_channel_ready=False,
            channel_evidence_class=EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
            acceptance_target_path="meta_cloud_api_direct",
            db_io_performed=False,
        )

    env_vars = {
        name: env_map.get(name, "")
        for name in (
            *META_READINESS_REQUIRED_ENV_NAMES,
            *D360_LEGACY_OBSERVABILITY_ENV_NAMES,
        )
    }
    config = evaluate_meta_config_present(env_vars)
    if config.get("d360_only_legacy_path"):
        return _report(
            PHASE_CHANNEL_HEALTH,
            ok=False,
            code=CODE_CHANNEL_D360_ONLY_LEGACY_PATH,
            tenant_id=tenant_id,
            credential_presence=creds,
            meta_config_present=False,
            operator_attested_channel_ready=False,
            channel_evidence_class=EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
            acceptance_target_path="meta_cloud_api_direct",
            meta_onboarding_target_path=META_ONBOARDING_TARGET_PATH,
            meta_onboarding_external_blocker=META_ONBOARDING_EXTERNAL_BLOCKER,
            channel_evidence_gaps=["d360_only_legacy_path"],
            note="BLOCK: 360dialog-only legacy path cannot satisfy Meta acceptance readiness",
            db_io_performed=False,
        )

    attestation = _resolve_webhook_attestation_artifact(attestation_artifact)
    attestation_key = (env_map.get(WEBHOOK_ATTESTATION_HMAC_KEY_ENV) or "").strip()
    pinned_revision = (env_map.get(PINNED_REVISION_ENV) or "").strip()
    deployment_id = (env_map.get(DEPLOYMENT_ID_ENV) or "").strip()
    backend_url = (env_map.get("BACKEND_URL") or "").strip()

    db_row_explicit = db_row is not _UNSET_DB_ROW
    if db_row is _UNSET_DB_ROW:
        if db_io_permitted:
            db_row = _load_whatsapp_connection_row(tenant_id)
        else:
            db_row = None

    evidence = evaluate_operator_attested_channel_ready(
        variables=env_vars,
        tenant_id=tenant_id,
        artifact=attestation,
        hmac_key=attestation_key,
        backend_url=backend_url,
        pinned_revision=pinned_revision,
        deployment_id=deployment_id,
        db_row=db_row,
    )
    if not evidence.get("operator_attested_channel_ready"):
        failure_code = _failure_code_for_channel_evidence(evidence)
        if (
            not db_io_permitted
            and not db_row_explicit
            and failure_code == CODE_DB_WA_BINDING_MISSING
        ):
            failure_code = db_gate_code or CODE_DATABASE_BINDING_REJECTED
        return _report(
            PHASE_CHANNEL_HEALTH,
            ok=False,
            code=failure_code,
            tenant_id=tenant_id,
            credential_presence=creds,
            meta_config_present=bool(evidence.get("meta_config_present")),
            operator_attested_channel_ready=False,
            channel_evidence_class=EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
            acceptance_target_path="meta_cloud_api_direct",
            meta_onboarding_target_path=META_ONBOARDING_TARGET_PATH,
            meta_onboarding_external_blocker=META_ONBOARDING_EXTERNAL_BLOCKER,
            webhook_attestation_gaps=evidence.get("webhook_attestation_gaps"),
            db_wa_binding_gaps=evidence.get("db_wa_binding_gaps"),
            channel_evidence_gaps=evidence.get("channel_evidence_gaps"),
            observed_callback_route=evidence.get("observed_callback_route"),
            observation_source=evidence.get("observation_source"),
            note="BLOCK: operator-attested webhook + DB binding evidence incomplete",
            db_io_performed=db_io_permitted,
        )

    return _report(
        PHASE_CHANNEL_HEALTH,
        ok=True,
        tenant_id=tenant_id,
        credential_presence=creds,
        meta_config_present=True,
        operator_attested_channel_ready=True,
        channel_evidence_class=EVIDENCE_CLASS_OPERATOR_OBSERVED_META_WEBHOOK,
        acceptance_target_path="meta_cloud_api_direct",
        meta_onboarding_target_path=META_ONBOARDING_TARGET_PATH,
        meta_onboarding_external_blocker=META_ONBOARDING_EXTERNAL_BLOCKER,
        observed_callback_route=evidence.get("observed_callback_route"),
        observation_source=evidence.get("observation_source"),
        d360_legacy_observability_only={
            name: creds.get(name) == "present" for name in D360_LEGACY_OBSERVABILITY_ENV_NAMES
        },
        execution_path_required=EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
        db_io_performed=db_io_permitted,
    )


def _execution_gates(*, phase: str, tenant_id: int) -> dict[str, Any] | None:
    if not env_flag_enabled(os.environ.get(MASTER_ENABLE_ENV)):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_ACCEPTANCE_NOT_ENABLED)
    if not env_flag_enabled(os.environ.get(EXECUTION_CONFIRM_ENV)):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_EXECUTION_NOT_CONFIRMED)
    signoff = verify_arch001_preprod_signoff_for_gate()
    if signoff.get("ok") is not True:
        return _report(
            PHASE_SUMMARY,
            ok=False,
            code=signoff.get("code") or CODE_ARCH001_SIGNOFF_MISSING,
        )
    if phase == PHASE_TENANT_33_LIMITED and not env_flag_enabled(
        os.environ.get(TENANT_1_PASS_CONFIRM_ENV)
    ):
        return _report(PHASE_SUMMARY, ok=False, code=CODE_TENANT_1_NOT_PASSED)
    if tenant_id not in {TENANT_1_INTENSIVE, TENANT_33_LIMITED}:
        return _report(PHASE_SUMMARY, ok=False, code=CODE_TENANT_NOT_ALLOWED)

    channel = execute_channel_health_preflight(tenant_id=tenant_id)
    gate_chain = evaluate_execution_gate_chain(
        tenant_id=tenant_id,
        arch001_signoff_ok=True,
        channel_health_ok=channel.get("ok") is True,
    )
    if not gate_chain.get("ok"):
        blockers = list(gate_chain.get("blockers") or [])
        primary = CODE_APPROVED_EGRESS_CONFIGURATION_MISSING
        if blockers:
            primary = blockers[0]
        return _report(
            PHASE_SUMMARY,
            ok=False,
            code=primary,
            execution_gate_blockers=blockers,
            execution_gate_proofs=gate_chain.get("proofs"),
        )
    return None


def execute_scenario_plan(
    *,
    phase: str,
    app_root: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """List scenarios only; actual execution belongs to the session runner."""
    if not dry_run:
        return _report(
            phase,
            ok=False,
            code=CODE_REAL_CHANNEL_REQUIRED,
            note=(
                "Use scripts.operators.real_channel_acceptance_session; this "
                "planning operator cannot execute or award channel evidence."
            ),
        )
    blocked = _execution_gates(phase=phase, tenant_id=TENANT_1_INTENSIVE if phase == PHASE_TENANT_1_INTENSIVE else TENANT_33_LIMITED)

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
            else "execution_requires_actual_provider_channel_and_test_device"
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
        "evidence_channel": execution_path,
        "execution_path_label": {
            EVIDENCE_CHANNEL_ACTUAL_PROVIDER: "actual_provider_channel",
            EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK: (
                "direct_signed_webhook_integration_probe_NOT_REAL_CHANNEL"
            ),
            EVIDENCE_CHANNEL_DIRECT_CODE_PROBE: "direct_code_probe_NOT_REAL_CHANNEL",
        }.get(execution_path, "unknown_NOT_REAL_CHANNEL"),
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
            f"unset {ARCH001_PREPROD_SIGNOFF_ARTIFACT_ENV}",
            f"unset {ARCH001_PREPROD_SIGNOFF_HMAC_KEY_ENV}",
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
        if arguments == ["webhook-attestation-template"]:
            from scripts.operators.meta_acceptance_channel_evidence_contract import (
                build_unsigned_webhook_attestation_template,
            )

            _emit(build_unsigned_webhook_attestation_template())
            return 0
        if arguments == ["arch001-unsigned-bundle-template"]:
            from scripts.operators.product_availability_preprod_synthetic_signoff_v2 import (
                build_unsigned_signoff_bundle_template,
            )

            _emit(build_unsigned_signoff_bundle_template())
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
