"""ARCH-001 preprod synthetic signoff v2 operator (governance-only).

Production signoff bundles ingest externally generated runtime-bound phase
artifacts. CI contract self-test produces ineligible bundles only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.operators import product_availability_truth_guard_shadow_observation as shadow_probe
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (
    BASELINE_IMAGE_DIGEST_ENV,
    BUNDLE_SCHEMA_VERSION,
    CANONICAL_DEPLOYMENT_ID_ENV,
    CANONICAL_SERVICE_ID_ENV,
    CANONICAL_SERVICE_NAME_ENV,
    CODE_ARTIFACT_UNREADABLE,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_BUNDLE_INVALID,
    CODE_BUNDLE_SIGNATURE_INVALID,
    CODE_COMMAND_INVALID,
    CODE_EVIDENCE_CLASS_INELIGIBLE,
    CODE_EXPECTED_IDENTITY_MISSING,
    CODE_HMAC_KEY_WEAK,
    CODE_IDENTITY_BINDING_MISMATCH,
    CODE_LEGACY_V1_NOT_SUFFICIENT,
    CODE_LIFECYCLE_PHASE_FAILED,
    CODE_LIFECYCLE_PHASE_MISSING,
    CODE_NEGATIVE_CONTROL_MISSING,
    CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS,
    CODE_PHASE_ARTIFACT_INVALID,
    CODE_POST_APPROVAL_NOT_PENDING,
    CODE_PROBE_FAILED,
    CODE_PRODUCTION_SYNTHETIC_MARKER,
    CODE_REVISION_FORMAT_INVALID,
    CODE_IMAGE_DIGEST_INVALID,
    CODE_RUNTIME_BINDING_MISMATCH,
    CODE_STABLE_COUNTERS_DRIFT,
    CODE_SUPERSEDED_WINDOWS_MISSING,
    CODE_TEARDOWN_PROOF_MISSING,
    CODE_TRAFFIC_CLAIM_INVALID,
    DEPLOYMENT_APP_ROOT,
    EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST,
    EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
    EXECUTION_MODE_IN_CONTAINER,
    EXPECTED_IMAGE_DIGEST_ENV,
    EXPECTED_MANIFEST_DIGEST_ENV,
    HMAC_DOMAIN_PREFIX,
    INITIATIVE_ID,
    ISOLATED_DEPLOYMENT_ID_ENV,
    ISOLATED_SERVICE_ID_ENV,
    ISOLATED_SERVICE_NAME_ENV,
    KNOWN_REJECTED_HMAC_KEYS,
    LEGACY_V1_SCHEMA_VERSION,
    LIFECYCLE_PHASES,
    NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION,
    NEGATIVE_CONTROL_EXPECTED_CODES,
    NEGATIVE_CONTROL_IDS,
    PHASE_ARTIFACT_SCHEMA_VERSION,
    PHASE_BASELINE,
    PHASE_BUNDLE,
    PHASE_CONTAINER_RESTART,
    PHASE_CONTRACT_SELF_TEST,
    PHASE_FRESH_PINNED_REDEPLOY,
    PHASE_LEGACY_V1_READ,
    PHASE_NEGATIVE_CONTROLS,
    PHASE_REPEAT_MATRIX_1,
    PHASE_REPEAT_MATRIX_2,
    PHASE_REPEAT_MATRIX_3,
    PHASE_VERIFY,
    PINNED_REVISION_ENV,
    POST_APPROVAL_PENDING,
    REPEAT_MATRIX_MIN_SPACING_SECONDS,
    SERVICE_ROLE_CANONICAL_CONTROL,
    SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
    TEARDOWN_PROOF_SCHEMA_VERSION,
    TRAFFIC_CLAIM,
    extract_stable_counters,
    is_legacy_v1_bundle,
    is_strong_hmac_key,
    validate_identity_binding_shape,
    validate_lifecycle_phase_identities,
    validate_lifecycle_phase_row,
    validate_matrix_payload,
    validate_negative_control_artifact,
    validate_phase_artifact,
    validate_phase_timestamp_order,
    validate_stable_counters,
    validate_superseded_windows,
    validate_teardown_proof,
    validate_bundle_timestamps,
    validate_image_digest_value,
    validate_production_image_digest,
    validate_revision_token,
)
from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
    read_checkout_revision,
)
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (
    SHADOW_MODE_ENV as SHADOW_PROBE_MODE_ENV,
    SHADOW_MODE_VALUE,
)

_REPO = Path(__file__).resolve().parents[2]
_CI_FIXTURE_HMAC_KEY = "ci-fixture-arch001-preprod-signoff-v2-key-32b"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(_canonical(payload) + "\n")
    sys.stdout.flush()


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {"phase": phase, "bundle_schema_version": BUNDLE_SCHEMA_VERSION, **payload}


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, CODE_ARTIFACT_UNREADABLE
    if not isinstance(payload, dict):
        return None, CODE_ARTIFACT_UNREADABLE
    return payload, None


def build_expected_identity_from_env(
    env: Mapping[str, str] | None = None,
    *,
    require_production_class: bool = True,
) -> tuple[dict[str, str] | None, list[str]]:
    source = env if env is not None else os.environ
    blockers: list[str] = []
    revision = (source.get(PINNED_REVISION_ENV) or source.get("NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION") or "").strip().lower()
    manifest = (source.get(EXPECTED_MANIFEST_DIGEST_ENV) or "").strip().lower()
    image_digest = (source.get(EXPECTED_IMAGE_DIGEST_ENV) or "").strip().lower()
    service_name = (source.get(ISOLATED_SERVICE_NAME_ENV) or "").strip()
    service_id = (source.get(ISOLATED_SERVICE_ID_ENV) or "").strip()
    deployment_id = (source.get(ISOLATED_DEPLOYMENT_ID_ENV) or "").strip()
    if not revision:
        blockers.append("pinned_revision_missing")
    elif require_production_class and not validate_revision_token(revision, require_full=True):
        blockers.append(CODE_REVISION_FORMAT_INVALID)
    if not manifest:
        blockers.append("manifest_digest_missing")
    if require_production_class:
        if not validate_production_image_digest(image_digest):
            blockers.append(CODE_IMAGE_DIGEST_INVALID)
    elif not image_digest or not validate_image_digest_value(image_digest, allow_absent=True):
        blockers.append("image_digest_missing")
    if not service_name:
        blockers.append("isolated_service_name_missing")
    if not service_id:
        blockers.append("isolated_service_id_missing")
    if not deployment_id:
        blockers.append("isolated_deployment_id_missing")
    if blockers:
        return None, blockers
    return {
        "pinned_target_revision": revision,
        "manifest_digest": manifest,
        "service_role": SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
        "service_name": service_name,
        "service_id": service_id,
        "deployment_id": deployment_id,
        "image_digest": image_digest,
    }, []


def build_expected_canonical_identity_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str] | None, list[str]]:
    source = env if env is not None else os.environ
    blockers: list[str] = []
    service_name = (source.get(CANONICAL_SERVICE_NAME_ENV) or "").strip()
    service_id = (source.get(CANONICAL_SERVICE_ID_ENV) or "").strip()
    deployment_id = (source.get(CANONICAL_DEPLOYMENT_ID_ENV) or "").strip()
    if not service_name:
        blockers.append("canonical_service_name_missing")
    if not service_id:
        blockers.append("canonical_service_id_missing")
    if not deployment_id:
        blockers.append("canonical_deployment_id_missing")
    if blockers:
        return None, blockers
    return {
        "service_role": SERVICE_ROLE_CANONICAL_CONTROL,
        "service_name": service_name,
        "service_id": service_id,
        "deployment_id": deployment_id,
    }, []


def resolve_baseline_image_digest_from_env(
    env: Mapping[str, str] | None = None,
    *,
    expected_identity: Mapping[str, str] | None = None,
    require_production_class: bool = True,
) -> str | None:
    source = env if env is not None else os.environ
    raw = (source.get(BASELINE_IMAGE_DIGEST_ENV) or "").strip().lower()
    if require_production_class:
        if validate_production_image_digest(raw):
            return raw
        return None
    if raw and validate_image_digest_value(raw, allow_absent=True):
        return raw
    if expected_identity:
        fallback = str(expected_identity.get("image_digest") or "").strip().lower()
        if validate_image_digest_value(fallback, allow_absent=True):
            return fallback
    return None


def verify_runtime_binding_for_gate(
    *,
    expected_identity: Mapping[str, str],
    app_root: Path | None = None,
) -> list[str]:
    blockers: list[str] = []
    root = shadow_probe.resolve_app_root(app_root or _REPO)
    manifest = shadow_probe.build_runtime_artifact_manifest(app_root=root)
    runtime_digest = str(manifest.get("manifest_digest") or "").strip().lower()
    if runtime_digest != str(expected_identity.get("manifest_digest") or "").strip().lower():
        blockers.append(CODE_RUNTIME_BINDING_MISMATCH)
    attestation = evaluate_runtime_revision_attestation(
        pinned_target_revision=str(expected_identity.get("pinned_target_revision") or ""),
        target_app_root=root,
    )
    if attestation.ok is not True:
        blockers.append(CODE_RUNTIME_BINDING_MISMATCH)
    return blockers


def ingest_phase_artifact_payload(
    payload: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, str] | None = None,
    baseline_deployment_id: str | None = None,
    redeploy_deployment_id: str | None = None,
    baseline_image_digest: str | None = None,
    redeploy_image_digest: str | None = None,
    require_production_provenance: bool = False,
    source_artifact_path: str | None = None,
) -> dict[str, Any]:
    blockers = validate_phase_artifact(
        payload,
        expected_identity=expected_identity,
        baseline_deployment_id=baseline_deployment_id,
        redeploy_deployment_id=redeploy_deployment_id,
        baseline_image_digest=baseline_image_digest,
        redeploy_image_digest=redeploy_image_digest,
        require_production_provenance=require_production_provenance,
    )
    row = dict(payload)
    row["ok"] = not blockers
    row["blockers"] = blockers
    if source_artifact_path:
        row["source_artifact_path"] = source_artifact_path
    return row


def ingest_phase_artifact(
    path: Path,
    *,
    expected_identity: Mapping[str, str] | None = None,
    baseline_deployment_id: str | None = None,
    redeploy_deployment_id: str | None = None,
) -> dict[str, Any]:
    payload, error = _load_json(path)
    if payload is None:
        return _report("ingest_phase_artifact", ok=False, code=error, path=str(path))
    row = ingest_phase_artifact_payload(
        payload,
        expected_identity=expected_identity,
        baseline_deployment_id=baseline_deployment_id,
        redeploy_deployment_id=redeploy_deployment_id,
        source_artifact_path=str(path),
    )
    return _report(
        "ingest_phase_artifact",
        ok=row.get("ok") is True,
        code=row.get("blockers", [None])[0] if row.get("blockers") else None,
        **{k: v for k, v in row.items() if k not in {"ok", "blockers"}},
        blockers=row.get("blockers") or [],
    )


def ingest_negative_control_artifact(
    path: Path,
    *,
    expected_identity: Mapping[str, str],
) -> dict[str, Any]:
    payload, error = _load_json(path)
    if payload is None:
        return _report("ingest_negative_control", ok=False, code=error, path=str(path))
    blockers = validate_negative_control_artifact(payload, expected_identity=expected_identity)
    return _report(
        "ingest_negative_control",
        ok=not blockers,
        code=blockers[0] if blockers else None,
        control_id=payload.get("control_id"),
        blocked=payload.get("blocked"),
        code_observed=payload.get("code"),
        identity_binding=payload.get("identity_binding"),
        blockers=blockers,
        source_artifact_path=str(path),
    )


def build_unsigned_bundle(
    *,
    identity_binding: Mapping[str, str],
    lifecycle_phases: list[Mapping[str, Any]],
    negative_controls: Mapping[str, Any],
    teardown_proof: Mapping[str, Any],
    superseded_invalid_windows: list[Mapping[str, Any]],
    evidence_class: str = EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
    eligible_for_signoff: bool = True,
) -> dict[str, Any]:
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "initiative_id": INITIATIVE_ID,
        "evidence_class": evidence_class,
        "eligible_for_signoff": eligible_for_signoff,
        "traffic_claim": TRAFFIC_CLAIM,
        "identity_binding": dict(identity_binding),
        "lifecycle_phases": list(lifecycle_phases),
        "negative_controls": dict(negative_controls),
        "stable_counter_reference": lifecycle_phases[0].get("stable_counters") if lifecycle_phases else {},
        "post_approval": {
            "canonical_shadow_canary": POST_APPROVAL_PENDING,
            "enforce_eligibility": POST_APPROVAL_PENDING,
        },
        "superseded_invalid_windows": list(superseded_invalid_windows),
        "teardown_proof": dict(teardown_proof),
        "signed_at_utc": _utc_now(),
    }


def sign_bundle(bundle: Mapping[str, Any], *, hmac_key: str, allow_fixture_keys: bool = False) -> dict[str, Any]:
    if not is_strong_hmac_key(hmac_key, allow_fixture_keys=allow_fixture_keys):
        raise ValueError(CODE_HMAC_KEY_WEAK)
    signed = dict(bundle)
    signed.pop("signature", None)
    material = HMAC_DOMAIN_PREFIX + _canonical(signed)
    digest = hmac.new(hmac_key.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
    signed["signature"] = f"hmac-sha256:{digest}"
    return signed


def verify_bundle_signature(bundle: Mapping[str, Any], *, hmac_key: str) -> bool:
    if not hmac_key:
        return False
    payload = dict(bundle)
    signature = str(payload.pop("signature", ""))
    if not signature.startswith("hmac-sha256:"):
        return False
    material = HMAC_DOMAIN_PREFIX + _canonical(payload)
    expected = f"hmac-sha256:{hmac.new(hmac_key.encode('utf-8'), material.encode('utf-8'), hashlib.sha256).hexdigest()}"
    return hmac.compare_digest(signature, expected)


def verify_preprod_signoff_v2_bundle(
    bundle: Mapping[str, Any],
    *,
    hmac_key: str,
    expected_identity: Mapping[str, str] | None = None,
    expected_canonical: Mapping[str, str] | None = None,
    require_production_class: bool = True,
    allow_fixture_keys: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []

    if require_production_class and not expected_identity:
        blockers.append(CODE_EXPECTED_IDENTITY_MISSING)

    if is_legacy_v1_bundle(bundle):
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_LEGACY_V1_NOT_SUFFICIENT,
            legacy_readable=True,
            blockers=[CODE_LEGACY_V1_NOT_SUFFICIENT],
        )

    if bundle.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
        blockers.append(CODE_BUNDLE_INVALID)
    if bundle.get("initiative_id") != INITIATIVE_ID:
        blockers.append(CODE_BUNDLE_INVALID)
    if bundle.get("traffic_claim") != TRAFFIC_CLAIM:
        blockers.append(CODE_TRAFFIC_CLAIM_INVALID)

    evidence_class = str(bundle.get("evidence_class") or "")
    eligible = bundle.get("eligible_for_signoff")
    if require_production_class:
        if evidence_class != EVIDENCE_CLASS_PRODUCTION_SIGNOFF:
            blockers.append(CODE_EVIDENCE_CLASS_INELIGIBLE)
        if eligible is not True:
            blockers.append(CODE_EVIDENCE_CLASS_INELIGIBLE)

    if not is_strong_hmac_key(hmac_key, allow_fixture_keys=allow_fixture_keys):
        blockers.append(CODE_HMAC_KEY_WEAK)
    elif not verify_bundle_signature(bundle, hmac_key=hmac_key):
        blockers.append(CODE_BUNDLE_SIGNATURE_INVALID)

    identity = bundle.get("identity_binding")
    if isinstance(identity, Mapping):
        blockers.extend(
            validate_identity_binding_shape(
                identity,
                require_production=require_production_class,
            )
        )
        if expected_identity:
            for key, value in expected_identity.items():
                if str(identity.get(key) or "") != str(value):
                    blockers.append(CODE_IDENTITY_BINDING_MISMATCH)
    else:
        blockers.append(CODE_IDENTITY_BINDING_MISMATCH)

    lifecycle_rows = bundle.get("lifecycle_phases")
    parsed_rows: list[Mapping[str, Any]] = []
    if not isinstance(lifecycle_rows, list):
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
        lifecycle_rows = []
    seen_phases: set[str] = set()
    baseline_deployment_id: str | None = None
    redeploy_deployment_id: str | None = None
    if isinstance(identity, Mapping):
        redeploy_deployment_id = str(identity.get("deployment_id") or "") or None
    phase_expected = (
        {k: v for k, v in expected_identity.items() if k not in {"deployment_id", "image_digest"}}
        if expected_identity
        else None
    )
    baseline_image_digest = resolve_baseline_image_digest_from_env(
        env,
        expected_identity=expected_identity,
        require_production_class=require_production_class,
    )
    redeploy_image_digest = str(expected_identity.get("image_digest") or "") if expected_identity else None
    production_provenance = require_production_class and evidence_class == EVIDENCE_CLASS_PRODUCTION_SIGNOFF
    for row in lifecycle_rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("phase") or "") == PHASE_BASELINE:
            binding = row.get("identity_binding")
            if isinstance(binding, Mapping):
                baseline_deployment_id = str(binding.get("deployment_id") or "") or None
                if not baseline_image_digest:
                    candidate = str(binding.get("image_digest") or "").strip().lower()
                    if validate_production_image_digest(candidate):
                        baseline_image_digest = candidate
        if str(row.get("phase") or "") == PHASE_FRESH_PINNED_REDEPLOY:
            attestation = row.get("lifecycle_attestation")
            if isinstance(attestation, Mapping):
                redeploy_deployment_id = str(attestation.get("new_deployment_id") or "") or redeploy_deployment_id
    if require_production_class:
        if not baseline_image_digest:
            blockers.append(CODE_IMAGE_DIGEST_INVALID)
        if not validate_production_image_digest(redeploy_image_digest):
            blockers.append(CODE_IMAGE_DIGEST_INVALID)
        if expected_identity and not validate_revision_token(
            expected_identity.get("pinned_target_revision"),
            require_full=True,
        ):
            blockers.append(CODE_REVISION_FORMAT_INVALID)
    for row in lifecycle_rows:
        if not isinstance(row, Mapping):
            blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
            continue
        phase = str(row.get("phase") or "")
        seen_phases.add(phase)
        if production_provenance and row.get("fixture_synthetic") is True:
            blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
        blockers.extend(
            validate_phase_artifact(
                row,
                expected_identity=phase_expected,
                baseline_deployment_id=baseline_deployment_id,
                redeploy_deployment_id=redeploy_deployment_id,
                baseline_image_digest=baseline_image_digest,
                redeploy_image_digest=redeploy_image_digest,
                require_production_provenance=production_provenance,
            )
        )
        if row.get("ok") is not True:
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
        parsed_rows.append(row)
    if seen_phases != set(LIFECYCLE_PHASES):
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
    blockers.extend(validate_phase_timestamp_order(parsed_rows))
    blockers.extend(validate_lifecycle_phase_identities(parsed_rows))

    teardown = bundle.get("teardown_proof")
    negative = bundle.get("negative_controls")
    if not isinstance(negative, Mapping) or negative.get("ok") is not True:
        blockers.append(CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS)
    else:
        controls = negative.get("controls")
        if not isinstance(controls, list):
            blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
        else:
            control_ids = {str(row.get("control_id") or "") for row in controls if isinstance(row, Mapping)}
            if control_ids != NEGATIVE_CONTROL_IDS:
                blockers.append(CODE_NEGATIVE_CONTROL_MISSING)
            if expected_identity:
                for row in controls:
                    if isinstance(row, Mapping):
                        if production_provenance and row.get("fixture_synthetic") is True:
                            blockers.append(CODE_PRODUCTION_SYNTHETIC_MARKER)
                        blockers.extend(
                            validate_negative_control_artifact(
                                row,
                                expected_identity=expected_identity,
                                require_production_provenance=production_provenance,
                            )
                        )

    blockers.extend(
        validate_bundle_timestamps(
            phases=parsed_rows,
            signed_at_utc=bundle.get("signed_at_utc"),
            teardown_proof=teardown if isinstance(teardown, Mapping) else None,
            negative_controls=negative if isinstance(negative, Mapping) else None,
        )
    )

    post_approval = bundle.get("post_approval")
    if not isinstance(post_approval, Mapping):
        blockers.append(CODE_POST_APPROVAL_NOT_PENDING)
    else:
        if post_approval.get("canonical_shadow_canary") != POST_APPROVAL_PENDING:
            blockers.append(CODE_POST_APPROVAL_NOT_PENDING)
        if post_approval.get("enforce_eligibility") != POST_APPROVAL_PENDING:
            blockers.append(CODE_POST_APPROVAL_NOT_PENDING)

    blockers.extend(
        validate_superseded_windows(
            bundle.get("superseded_invalid_windows"),
            require_migration_windows=production_provenance,
            require_production_provenance=production_provenance,
        )
    )
    if isinstance(teardown, Mapping):
        blockers.extend(
            validate_teardown_proof(
                teardown,
                expected_isolated=expected_identity,
                expected_canonical=expected_canonical,
                require_production_provenance=production_provenance,
            )
        )
    else:
        blockers.append(CODE_TEARDOWN_PROOF_MISSING)

    unique_blockers = sorted(set(blockers))
    return _report(
        PHASE_VERIFY,
        ok=not unique_blockers,
        code=unique_blockers[0] if unique_blockers else None,
        blockers=unique_blockers,
        preprod_signoff_valid=not unique_blockers,
        post_approval_shadow_canary=POST_APPROVAL_PENDING,
        enforce_eligibility=POST_APPROVAL_PENDING,
    )


def verify_arch001_preprod_signoff_for_gate(
    *,
    env: Mapping[str, str] | None = None,
    app_root: Path | None = None,
) -> dict[str, Any]:
    source = env if env is not None else os.environ
    artifact_path = (source.get(SIGNOFF_ARTIFACT_ENV) or "").strip()
    hmac_key = (source.get(SIGNOFF_HMAC_KEY_ENV) or "").strip()
    if not artifact_path or not hmac_key:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_ARCH001_SIGNOFF_MISSING,
            blockers=[CODE_ARCH001_SIGNOFF_MISSING],
        )
    expected_identity, identity_blockers = build_expected_identity_from_env(
        source,
        require_production_class=True,
    )
    if identity_blockers:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_EXPECTED_IDENTITY_MISSING,
            blockers=identity_blockers,
        )
    expected_canonical, canonical_blockers = build_expected_canonical_identity_from_env(source)
    if canonical_blockers:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_EXPECTED_IDENTITY_MISSING,
            blockers=canonical_blockers,
        )
    runtime_blockers = verify_runtime_binding_for_gate(
        expected_identity=expected_identity,
        app_root=app_root,
    )
    if runtime_blockers:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_RUNTIME_BINDING_MISMATCH,
            blockers=runtime_blockers,
        )
    payload, error = _load_json(Path(artifact_path))
    if payload is None:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=error or CODE_ARTIFACT_UNREADABLE,
            blockers=[error or CODE_ARTIFACT_UNREADABLE],
        )
    return verify_preprod_signoff_v2_bundle(
        payload,
        hmac_key=hmac_key,
        expected_identity=expected_identity,
        expected_canonical=expected_canonical,
        require_production_class=True,
        env=source,
    )


def load_and_verify_artifact_from_env(
    *,
    artifact_env: str = SIGNOFF_ARTIFACT_ENV,
    hmac_key_env: str = SIGNOFF_HMAC_KEY_ENV,
    expected_identity: Mapping[str, str] | None = None,
    expected_canonical: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    require_production_class: bool = True,
    app_root: Path | None = None,
) -> dict[str, Any]:
    if expected_identity is None:
        return verify_arch001_preprod_signoff_for_gate(env=env, app_root=app_root)
    if require_production_class and not expected_identity:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_EXPECTED_IDENTITY_MISSING,
            blockers=[CODE_EXPECTED_IDENTITY_MISSING],
        )
    source = env if env is not None else os.environ
    artifact_path = (source.get(artifact_env) or "").strip()
    hmac_key = (source.get(hmac_key_env) or "").strip()
    if not artifact_path or not hmac_key:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=CODE_BUNDLE_INVALID,
            blockers=[CODE_BUNDLE_INVALID],
        )
    if require_production_class:
        runtime_blockers = verify_runtime_binding_for_gate(
            expected_identity=expected_identity,
            app_root=app_root,
        )
        if runtime_blockers:
            return _report(
                PHASE_VERIFY,
                ok=False,
                code=CODE_RUNTIME_BINDING_MISMATCH,
                blockers=runtime_blockers,
            )
    payload, error = _load_json(Path(artifact_path))
    if payload is None:
        return _report(
            PHASE_VERIFY,
            ok=False,
            code=error or CODE_ARTIFACT_UNREADABLE,
            blockers=[error or CODE_ARTIFACT_UNREADABLE],
        )
    return verify_preprod_signoff_v2_bundle(
        payload,
        hmac_key=hmac_key,
        expected_identity=expected_identity,
        expected_canonical=expected_canonical,
        require_production_class=require_production_class,
        env=source,
    )


def assemble_bundle_from_artifacts(
    *,
    phase_dir: Path,
    teardown_path: Path,
    negative_controls_dir: Path,
    superseded_windows_path: Path,
    expected_identity: Mapping[str, str],
    expected_canonical: Mapping[str, str],
    hmac_key: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not is_strong_hmac_key(hmac_key):
        return _report(PHASE_BUNDLE, ok=False, code=CODE_HMAC_KEY_WEAK)

    if not validate_revision_token(expected_identity.get("pinned_target_revision"), require_full=True):
        return _report(PHASE_BUNDLE, ok=False, code=CODE_REVISION_FORMAT_INVALID)
    if not validate_production_image_digest(expected_identity.get("image_digest")):
        return _report(PHASE_BUNDLE, ok=False, code=CODE_IMAGE_DIGEST_INVALID)

    superseded_payload, superseded_error = _load_json(superseded_windows_path)
    if superseded_payload is None:
        return _report(PHASE_BUNDLE, ok=False, code=superseded_error)
    superseded_rows = superseded_payload.get("windows") if isinstance(superseded_payload, Mapping) else superseded_payload
    if not isinstance(superseded_rows, list):
        return _report(PHASE_BUNDLE, ok=False, code=CODE_SUPERSEDED_WINDOWS_MISSING)

    lifecycle_rows: list[dict[str, Any]] = []
    baseline_deployment_id: str | None = None
    redeploy_deployment_id = expected_identity.get("deployment_id")
    phase_expected = {
        key: value
        for key, value in expected_identity.items()
        if key not in {"deployment_id", "image_digest"}
    }
    baseline_image_digest = resolve_baseline_image_digest_from_env(
        env,
        expected_identity=expected_identity,
        require_production_class=True,
    )
    redeploy_image_digest = str(expected_identity.get("image_digest") or "")
    if not baseline_image_digest:
        return _report(PHASE_BUNDLE, ok=False, code=CODE_IMAGE_DIGEST_INVALID)
    for phase in LIFECYCLE_PHASES:
        artifact_path = phase_dir / f"{phase}.json"
        payload, error = _load_json(artifact_path)
        if payload is None:
            return _report(PHASE_BUNDLE, ok=False, code=error, lifecycle_phase=phase)
        row = ingest_phase_artifact_payload(
            payload,
            expected_identity=phase_expected,
            baseline_deployment_id=baseline_deployment_id,
            redeploy_deployment_id=str(redeploy_deployment_id or ""),
            baseline_image_digest=baseline_image_digest,
            redeploy_image_digest=redeploy_image_digest,
            require_production_provenance=True,
            source_artifact_path=str(artifact_path),
        )
        if row.get("ok") is not True:
            return _report(
                PHASE_BUNDLE,
                ok=False,
                code=row.get("blockers", [None])[0],
                lifecycle_phase=phase,
                blockers=row.get("blockers"),
            )
        lifecycle_rows.append(row)
        if phase == PHASE_BASELINE:
            binding = row.get("identity_binding")
            if isinstance(binding, Mapping):
                baseline_deployment_id = str(binding.get("deployment_id") or "")

    teardown_payload, teardown_error = _load_json(teardown_path)
    if teardown_payload is None:
        return _report(PHASE_BUNDLE, ok=False, code=teardown_error)
    teardown_blockers = validate_teardown_proof(
        teardown_payload,
        expected_isolated=expected_identity,
        expected_canonical=expected_canonical,
        require_production_provenance=True,
    )
    if teardown_blockers:
        return _report(PHASE_BUNDLE, ok=False, code=teardown_blockers[0], blockers=teardown_blockers)

    controls: list[dict[str, Any]] = []
    for control_id in sorted(NEGATIVE_CONTROL_IDS):
        control_path = negative_controls_dir / f"{control_id}.json"
        ingested = ingest_negative_control_artifact(
            control_path,
            expected_identity=expected_identity,
        )
        if ingested.get("ok") is not True:
            return _report(PHASE_BUNDLE, ok=False, code=ingested.get("code"), control_id=control_id)
        control_payload, _ = _load_json(control_path)
        if control_payload is None:
            return _report(PHASE_BUNDLE, ok=False, code=CODE_NEGATIVE_CONTROL_MISSING, control_id=control_id)
        control_blockers = validate_negative_control_artifact(
            control_payload,
            expected_identity=expected_identity,
            require_production_provenance=True,
        )
        if control_blockers:
            return _report(PHASE_BUNDLE, ok=False, code=control_blockers[0], control_id=control_id)
        controls.append(
            {
                "negative_control_schema_version": NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION,
                "control_id": control_id,
                "blocked": True,
                "code": ingested.get("code_observed"),
                "expected_code": NEGATIVE_CONTROL_EXPECTED_CODES[control_id],
                "identity_binding": ingested.get("identity_binding"),
                "executed_at_utc": control_payload.get("executed_at_utc"),
                "execution_mode": control_payload.get("execution_mode"),
                "target_app_root": control_payload.get("target_app_root"),
                "source_artifact_path": ingested.get("source_artifact_path"),
            }
        )
    negative_controls = {
        "ok": True,
        "controls": controls,
    }

    unsigned = build_unsigned_bundle(
        identity_binding=expected_identity,
        lifecycle_phases=lifecycle_rows,
        negative_controls=negative_controls,
        teardown_proof=teardown_payload,
        superseded_invalid_windows=superseded_rows,
        evidence_class=EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
        eligible_for_signoff=True,
    )
    signed = sign_bundle(unsigned, hmac_key=hmac_key)
    verify = verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=hmac_key,
        expected_identity=expected_identity,
        expected_canonical=expected_canonical,
        require_production_class=True,
        env=env,
    )
    return _report(
        PHASE_BUNDLE,
        ok=verify.get("ok") is True,
        bundle=signed,
        verify=verify,
        evidence_class=EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
        eligible_for_signoff=True,
    )


def _build_ci_matrix(app_root: Path) -> dict[str, Any]:
    os.environ[SHADOW_PROBE_MODE_ENV] = SHADOW_MODE_VALUE
    try:
        matrix = shadow_probe.execute_synthetic_matrix_probe(app_root=app_root)
    finally:
        os.environ.pop(SHADOW_PROBE_MODE_ENV, None)
    return matrix


def _build_ci_phase_artifact(
    *,
    phase: str,
    start: datetime,
    offset_minutes: int,
    identity_binding: Mapping[str, str],
    matrix: Mapping[str, Any],
    baseline_deployment_id: str | None = None,
    redeploy_deployment_id: str | None = None,
) -> dict[str, Any]:
    executed_at = (start + timedelta(minutes=offset_minutes)).replace(microsecond=0).isoformat()
    attestation: dict[str, Any]
    binding = dict(identity_binding)
    if phase == PHASE_BASELINE:
        binding["deployment_id"] = baseline_deployment_id or binding["deployment_id"]
        attestation = {"phase": phase, "action": "initial_deploy"}
    elif phase == PHASE_CONTAINER_RESTART:
        binding["deployment_id"] = baseline_deployment_id or binding["deployment_id"]
        attestation = {
            "phase": phase,
            "action": "container_restart",
            "restart_evidence": {
                "prior_container_id": "ci-prior-container",
                "new_container_id": "ci-new-container",
                "restart_completed_at_utc": executed_at,
            },
        }
    elif phase == PHASE_FRESH_PINNED_REDEPLOY:
        prior = baseline_deployment_id or binding["deployment_id"]
        new = redeploy_deployment_id or "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        binding["deployment_id"] = new
        attestation = {
            "phase": phase,
            "action": "fresh_pinned_redeploy",
            "prior_deployment_id": prior,
            "new_deployment_id": new,
        }
    else:
        redeploy = redeploy_deployment_id or binding["deployment_id"]
        binding["deployment_id"] = redeploy
        seq = int(phase.rsplit("_", 1)[-1])
        attestation = {
            "phase": phase,
            "action": "repeat_matrix",
            "sequence": seq,
            "deployment_id": redeploy,
        }
    return {
        "phase_artifact_schema_version": PHASE_ARTIFACT_SCHEMA_VERSION,
        "phase": phase,
        "execution_mode": EXECUTION_MODE_IN_CONTAINER,
        "target_app_root": DEPLOYMENT_APP_ROOT,
        "executed_at_utc": executed_at,
        "identity_binding": binding,
        "matrix": matrix,
        "stable_counters": extract_stable_counters(matrix),
        "lifecycle_attestation": attestation,
        "isolated_service_constraints": {
            "no_domains": True,
            "no_provider_credentials": True,
        },
        "fixture_synthetic": True,
    }


def execute_contract_self_test(*, app_root: Path | None = None) -> dict[str, Any]:
    root = shadow_probe.resolve_app_root(app_root)
    manifest = shadow_probe.build_runtime_artifact_manifest(app_root=root)
    matrix = _build_ci_matrix(root)
    matrix_blockers = validate_matrix_payload(matrix)
    if matrix_blockers:
        return _report(PHASE_CONTRACT_SELF_TEST, ok=False, code=matrix_blockers[0], blockers=matrix_blockers)

    baseline_id = "cbe93c7b-5891-49de-8bd0-5588acad14b5"
    redeploy_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    checkout = read_checkout_revision(root)
    pinned_revision = checkout[:40].lower() if checkout else "a8487b25" + ("0" * 32)
    identity = {
        "pinned_target_revision": pinned_revision,
        "manifest_digest": manifest["manifest_digest"],
        "service_role": SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
        "service_name": "nahla-arch001-shadow",
        "service_id": "11111111-1111-4111-8111-111111111111",
        "deployment_id": redeploy_id,
        "image_digest": "absent",
    }
    start = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    offsets = [0, 5, 10, 30, 45, 60]
    lifecycle_rows: list[dict[str, Any]] = []
    for phase, offset in zip(LIFECYCLE_PHASES, offsets):
        artifact = _build_ci_phase_artifact(
            phase=phase,
            start=start,
            offset_minutes=offset,
            identity_binding=identity,
            matrix=matrix,
            baseline_deployment_id=baseline_id,
            redeploy_deployment_id=redeploy_id,
        )
        row = ingest_phase_artifact_payload(
            artifact,
            expected_identity={k: v for k, v in identity.items() if k != "deployment_id"},
            baseline_deployment_id=baseline_id,
            redeploy_deployment_id=redeploy_id,
        )
        lifecycle_rows.append(row)

    control_executed_at = _utc_now()
    controls = [
        {
            "negative_control_schema_version": NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION,
            "control_id": control_id,
            "blocked": True,
            "code": NEGATIVE_CONTROL_EXPECTED_CODES[control_id],
            "executed_at_utc": control_executed_at,
            "execution_mode": EXECUTION_MODE_IN_CONTAINER,
            "target_app_root": DEPLOYMENT_APP_ROOT,
            "identity_binding": identity,
            "fixture_synthetic": True,
        }
        for control_id in sorted(NEGATIVE_CONTROL_IDS)
    ]
    negative_controls = {"ok": True, "controls": controls}
    teardown = {
        "teardown_proof_schema_version": TEARDOWN_PROOF_SCHEMA_VERSION,
        "isolated_service": {
            "guard_mode": "off",
            "service_state": "stopped",
            "verified_at_utc": _utc_now(),
            "service_role": SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
            "service_name": identity["service_name"],
            "service_id": identity["service_id"],
            "deployment_id": redeploy_id,
        },
        "canonical_control": {
            "guard_mode": "off",
            "service_role": SERVICE_ROLE_CANONICAL_CONTROL,
            "service_name": "nahla-saas",
            "service_id": "686b36c5-a926-4e58-912a-5e9d13fbc2e7",
            "deployment_id": "33333333-3333-4333-8333-333333333333",
            "verified_at_utc": _utc_now(),
        },
        "fixture_synthetic": True,
    }
    unsigned = build_unsigned_bundle(
        identity_binding=identity,
        lifecycle_phases=lifecycle_rows,
        negative_controls=negative_controls,
        teardown_proof=teardown,
        superseded_invalid_windows=[
            {
                "window_id": "arch001-48h-zero-traffic-v1",
                "reason": "superseded_by_preprod_synthetic_signoff_v2",
                "active": False,
                "superseded_at_utc": _utc_now(),
            }
        ],
        evidence_class=EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST,
        eligible_for_signoff=False,
    )
    signed = sign_bundle(unsigned, hmac_key=_CI_FIXTURE_HMAC_KEY, allow_fixture_keys=True)
    verify = verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=_CI_FIXTURE_HMAC_KEY,
        expected_identity=identity,
        require_production_class=False,
        allow_fixture_keys=True,
    )
    gate_reject = verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=_CI_FIXTURE_HMAC_KEY,
        expected_identity=identity,
        require_production_class=True,
        allow_fixture_keys=True,
    )
    return _report(
        PHASE_CONTRACT_SELF_TEST,
        ok=verify.get("ok") is True and gate_reject.get("ok") is not True,
        bundle=signed,
        verify=verify,
        gate_reject=gate_reject,
        evidence_class=EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST,
        eligible_for_signoff=False,
        repeat_matrix_min_spacing_seconds=REPEAT_MATRIX_MIN_SPACING_SECONDS,
    )


def read_legacy_v1_bundle(path: Path) -> dict[str, Any]:
    payload, error = _load_json(path)
    if payload is None:
        return _report(PHASE_LEGACY_V1_READ, ok=False, code=error)
    readable = is_legacy_v1_bundle(payload)
    return _report(
        PHASE_LEGACY_V1_READ,
        ok=readable,
        code=None if readable else CODE_BUNDLE_INVALID,
        legacy_schema_version=LEGACY_V1_SCHEMA_VERSION if readable else None,
        sufficient_for_preprod=False,
        note="v1 evidence remains historically readable but cannot unlock preprod gates",
    )


def _parse_assemble_bundle_flags(arguments: list[str]) -> dict[str, str] | None:
    flags: dict[str, str | None] = {
        "phase_dir": None,
        "teardown": None,
        "negative_controls_dir": None,
        "superseded_windows": None,
        "output": None,
    }
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--phase-dir" and index + 1 < len(arguments):
            flags["phase_dir"] = arguments[index + 1]
            index += 2
            continue
        if token == "--teardown" and index + 1 < len(arguments):
            flags["teardown"] = arguments[index + 1]
            index += 2
            continue
        if token == "--negative-controls-dir" and index + 1 < len(arguments):
            flags["negative_controls_dir"] = arguments[index + 1]
            index += 2
            continue
        if token == "--superseded-windows" and index + 1 < len(arguments):
            flags["superseded_windows"] = arguments[index + 1]
            index += 2
            continue
        if token == "--output" and index + 1 < len(arguments):
            flags["output"] = arguments[index + 1]
            index += 2
            continue
        return None
    if not flags["phase_dir"] or not flags["teardown"] or not flags["negative_controls_dir"] or not flags["superseded_windows"]:
        return None
    return {key: str(value) for key, value in flags.items() if value is not None}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["assemble-bundle"]:
            flags = _parse_assemble_bundle_flags(arguments[1:])
            if flags is None:
                raise ValueError(CODE_COMMAND_INVALID)
            expected, blockers = build_expected_identity_from_env()
            if blockers:
                _emit(_report(PHASE_BUNDLE, ok=False, code=CODE_EXPECTED_IDENTITY_MISSING, blockers=blockers))
                return 1
            expected_canonical, canonical_blockers = build_expected_canonical_identity_from_env()
            if canonical_blockers:
                _emit(_report(PHASE_BUNDLE, ok=False, code=CODE_EXPECTED_IDENTITY_MISSING, blockers=canonical_blockers))
                return 1
            hmac_key = (os.environ.get(SIGNOFF_HMAC_KEY_ENV) or "").strip()
            if not hmac_key:
                _emit(_report(PHASE_BUNDLE, ok=False, code=CODE_ARCH001_SIGNOFF_MISSING, blockers=[CODE_ARCH001_SIGNOFF_MISSING]))
                return 1
            result = assemble_bundle_from_artifacts(
                phase_dir=Path(flags["phase_dir"]),
                teardown_path=Path(flags["teardown"]),
                negative_controls_dir=Path(flags["negative_controls_dir"]),
                superseded_windows_path=Path(flags["superseded_windows"]),
                expected_identity=expected,
                expected_canonical=expected_canonical,
                hmac_key=hmac_key,
            )
            if result.get("ok") and flags.get("output"):
                Path(flags["output"]).write_text(
                    json.dumps(result["bundle"], indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments == ["contract-self-test"]:
            result = execute_contract_self_test(app_root=_REPO)
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments[:1] == ["ingest-phase-artifact"] and len(arguments) == 2:
            expected, blockers = build_expected_identity_from_env()
            if blockers:
                _emit(_report("ingest_phase_artifact", ok=False, code=CODE_EXPECTED_IDENTITY_MISSING, blockers=blockers))
                return 1
            result = ingest_phase_artifact(Path(arguments[1]), expected_identity=expected)
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments[:1] == ["verify-bundle"] and len(arguments) == 2:
            hmac_key = (os.environ.get(SIGNOFF_HMAC_KEY_ENV) or "").strip()
            payload, error = _load_json(Path(arguments[1]))
            if payload is None:
                _emit(_report(PHASE_VERIFY, ok=False, code=error))
                return 1
            expected, blockers = build_expected_identity_from_env()
            if blockers:
                _emit(_report(PHASE_VERIFY, ok=False, code=CODE_EXPECTED_IDENTITY_MISSING, blockers=blockers))
                return 1
            expected_canonical, canonical_blockers = build_expected_canonical_identity_from_env()
            if canonical_blockers:
                _emit(_report(PHASE_VERIFY, ok=False, code=CODE_EXPECTED_IDENTITY_MISSING, blockers=canonical_blockers))
                return 1
            result = verify_preprod_signoff_v2_bundle(
                payload,
                hmac_key=hmac_key,
                expected_identity=expected,
                expected_canonical=expected_canonical,
                require_production_class=True,
                env=os.environ,
            )
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments[:1] == ["verify-legacy-v1"] and len(arguments) == 2:
            _emit(read_legacy_v1_bundle(Path(arguments[1])))
            return 0
        if arguments == ["verify-artifact-env"]:
            result = verify_arch001_preprod_signoff_for_gate()
            _emit(result)
            return 0 if result.get("ok") else 1
        raise ValueError(CODE_COMMAND_INVALID)
    except ValueError:
        _emit(_report(PHASE_BUNDLE, ok=False, code=CODE_COMMAND_INVALID))
        return 2
    except BaseException:
        _emit(_report(PHASE_BUNDLE, ok=False, code=CODE_PROBE_FAILED))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
