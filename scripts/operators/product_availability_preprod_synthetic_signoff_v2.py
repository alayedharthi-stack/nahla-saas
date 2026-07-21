"""ARCH-001 preprod synthetic signoff v2 operator (governance-only).

Phase/lifecycle-based synthetic matrix signoff with HMAC-signed bundles.
No customer text, no provider calls, no DB writes. Does not enable shadow,
acceptance, or enforce modes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.operators import product_availability_truth_guard_shadow_observation as shadow_probe
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (
    BUNDLE_SCHEMA_VERSION,
    CODE_BUNDLE_INVALID,
    CODE_BUNDLE_SIGNATURE_INVALID,
    CODE_COMMAND_INVALID,
    CODE_IDENTITY_BINDING_MISMATCH,
    CODE_LEGACY_V1_NOT_SUFFICIENT,
    CODE_LIFECYCLE_PHASE_FAILED,
    CODE_LIFECYCLE_PHASE_MISSING,
    CODE_MATRIX_INVARIANT_VIOLATION,
    CODE_NEGATIVE_CONTROL_MISSING,
    CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS,
    CODE_POST_APPROVAL_NOT_PENDING,
    CODE_PROBE_FAILED,
    CODE_STABLE_COUNTERS_DRIFT,
    CODE_SUPERSEDED_WINDOW_ACTIVE,
    CODE_TEARDOWN_PROOF_MISSING,
    CODE_TRAFFIC_CLAIM_INVALID,
    DEPLOYMENT_APP_ROOT,
    ENFORCE_MODE_VALUE,
    INITIATIVE_ID,
    LEGACY_V1_SCHEMA_VERSION,
    LIFECYCLE_PHASES,
    NEGATIVE_CONTROL_EXPECTED_CODES,
    NEGATIVE_CONTROL_IDS,
    NEGATIVE_ENFORCE_ENABLED,
    NEGATIVE_OUTSIDE_APP,
    NEGATIVE_WRONG_MANIFEST,
    NEGATIVE_WRONG_REVISION,
    PHASE_BASELINE,
    PHASE_BUNDLE,
    PHASE_LEGACY_V1_READ,
    PHASE_NEGATIVE_CONTROLS,
    PHASE_VERIFY,
    POST_APPROVAL_PENDING,
    SHADOW_MODE_ENV,
    SHADOW_MODE_VALUE,
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
    TRAFFIC_CLAIM,
    extract_stable_counters,
    is_legacy_v1_bundle,
    validate_identity_binding,
    validate_lifecycle_phase_row,
)
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (
    CODE_ARTIFACT_MANIFEST_MISMATCH,
    CODE_ENFORCE_MODE_ENABLED as SHADOW_CODE_ENFORCE_MODE_ENABLED,
    CODE_RUNTIME_EXECUTION_REQUIRED,
    CODE_SHADOW_MODE_NOT_ENABLED,
    ENFORCE_MODE_VALUE as SHADOW_ENFORCE_MODE_VALUE,
    SHADOW_MODE_ENV as SHADOW_PROBE_MODE_ENV,
)

_REPO = Path(__file__).resolve().parents[2]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(_canonical(payload) + "\n")
    sys.stdout.flush()


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {"phase": phase, "bundle_schema_version": BUNDLE_SCHEMA_VERSION, **payload}


def resolve_app_root(artifact_root: Path | None = None) -> Path:
    return shadow_probe.resolve_app_root(artifact_root)


def build_identity_binding(
    *,
    pinned_target_revision: str,
    manifest_digest: str,
    deployment_id: str,
    image_digest: str | None = None,
) -> dict[str, str]:
    from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (
        CANONICAL_SERVICE_ID,
        CANONICAL_SERVICE_NAME,
    )

    binding = {
        "pinned_target_revision": pinned_target_revision.strip().lower(),
        "manifest_digest": manifest_digest.strip().lower(),
        "service_name": CANONICAL_SERVICE_NAME,
        "service_id": CANONICAL_SERVICE_ID,
        "deployment_id": deployment_id.strip(),
        "image_digest": (image_digest or "").strip().lower() or "absent",
    }
    blockers = validate_identity_binding(binding)
    if blockers:
        raise ValueError(blockers[0])
    return binding


def execute_lifecycle_matrix_phase(
    *,
    phase: str,
    app_root: Path | None = None,
    dependency_fault: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if phase not in LIFECYCLE_PHASES:
        return _report(phase, ok=False, code=CODE_LIFECYCLE_PHASE_MISSING)

    root = resolve_app_root(app_root)
    os.environ[SHADOW_PROBE_MODE_ENV] = SHADOW_MODE_VALUE
    try:
        matrix = shadow_probe.execute_synthetic_matrix_probe(app_root=root)
    finally:
        os.environ.pop(SHADOW_PROBE_MODE_ENV, None)

    blockers = validate_lifecycle_phase_row(
        {"phase": phase, "ok": matrix.get("ok"), "matrix": matrix, "dependency_fault": dependency_fault}
    )
    return _report(
        phase,
        ok=not blockers,
        code=blockers[0] if blockers else None,
        executed_at_utc=_utc_now(),
        matrix=matrix,
        stable_counters=extract_stable_counters(matrix),
        dependency_fault=dependency_fault,
        blockers=blockers,
    )


def execute_negative_controls(*, app_root: Path | None = None) -> dict[str, Any]:
    root = resolve_app_root(app_root)
    manifest = shadow_probe.build_runtime_artifact_manifest(app_root=root)
    results: list[dict[str, Any]] = []

    wrong_manifest = shadow_probe.execute_runtime_matrix_probe(
        pinned_target_revision="a8487b25",
        expected_manifest_digest="0" * 64,
        app_root=root,
        required_runtime_root=root,
    )
    results.append(
        _negative_row(
            NEGATIVE_WRONG_MANIFEST,
            ok=wrong_manifest.get("ok") is False,
            code=str(wrong_manifest.get("code") or ""),
        )
    )

    wrong_revision = shadow_probe.gate_runtime_revision_attestation(
        pinned_target_revision="deadbeef",
        target_app_root=root,
    )
    results.append(
        _negative_row(
            NEGATIVE_WRONG_REVISION,
            ok=wrong_revision.get("ok") is False,
            code=str(wrong_revision.get("code") or ""),
        )
    )

    outside_app = shadow_probe.execute_runtime_matrix_probe(
        pinned_target_revision="a8487b25",
        expected_manifest_digest=manifest["manifest_digest"],
        app_root=root,
        required_runtime_root=Path(DEPLOYMENT_APP_ROOT),
    )
    results.append(
        _negative_row(
            NEGATIVE_OUTSIDE_APP,
            ok=outside_app.get("ok") is False,
            code=str(outside_app.get("code") or ""),
        )
    )

    os.environ[SHADOW_PROBE_MODE_ENV] = SHADOW_ENFORCE_MODE_VALUE
    try:
        enforce = _evaluate_enforce_mode_block()
    finally:
        os.environ.pop(SHADOW_PROBE_MODE_ENV, None)
    results.append(
        _negative_row(
            NEGATIVE_ENFORCE_ENABLED,
            ok=enforce.get("ok") is True,
            code=str(enforce.get("code") or ""),
        )
    )

    missing = NEGATIVE_CONTROL_IDS - {row["control_id"] for row in results}
    unexpected_pass = [
        row["control_id"]
        for row in results
        if row.get("ok") is not True
        or row.get("code") != NEGATIVE_CONTROL_EXPECTED_CODES[row["control_id"]]
    ]
    ok = not missing and not unexpected_pass
    code = None
    if missing:
        code = CODE_NEGATIVE_CONTROL_MISSING
    elif unexpected_pass:
        code = CODE_NEGATIVE_CONTROL_UNEXPECTED_PASS
    return _report(
        PHASE_NEGATIVE_CONTROLS,
        ok=ok,
        code=code,
        controls=results,
        missing_controls=sorted(missing),
        unexpected_pass_controls=sorted(unexpected_pass),
    )


def _evaluate_enforce_mode_block() -> dict[str, Any]:
    """Governance negative control — enforce mode must block preprod signoff."""
    mode = os.environ.get(SHADOW_PROBE_MODE_ENV, "off").strip().lower()
    if mode == SHADOW_ENFORCE_MODE_VALUE:
        return {"ok": True, "code": SHADOW_CODE_ENFORCE_MODE_ENABLED}
    return {"ok": False, "code": CODE_SHADOW_MODE_NOT_ENABLED}


def _negative_row(control_id: str, *, ok: bool, code: str) -> dict[str, Any]:
    expected = NEGATIVE_CONTROL_EXPECTED_CODES[control_id]
    return {
        "control_id": control_id,
        "ok": ok and code == expected,
        "expected_code": expected,
        "code": code,
        "blocked": code == expected,
    }


def _validate_stable_counters_across_phases(phases: list[Mapping[str, Any]]) -> list[str]:
    if not phases:
        return [CODE_STABLE_COUNTERS_DRIFT]
    reference = phases[0].get("stable_counters")
    if not isinstance(reference, Mapping):
        return [CODE_STABLE_COUNTERS_DRIFT]
    blockers: list[str] = []
    for row in phases[1:]:
        counters = row.get("stable_counters")
        if counters != reference:
            blockers.append(CODE_STABLE_COUNTERS_DRIFT)
    return blockers


def build_unsigned_bundle(
    *,
    identity_binding: Mapping[str, str],
    lifecycle_phases: list[Mapping[str, Any]],
    negative_controls: Mapping[str, Any],
    superseded_invalid_windows: list[Mapping[str, Any]] | None = None,
    teardown_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stable_reference = lifecycle_phases[0].get("stable_counters") if lifecycle_phases else {}
    return {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "initiative_id": INITIATIVE_ID,
        "traffic_claim": TRAFFIC_CLAIM,
        "identity_binding": dict(identity_binding),
        "lifecycle_phases": list(lifecycle_phases),
        "negative_controls": dict(negative_controls),
        "stable_counter_reference": stable_reference,
        "post_approval": {
            "canonical_shadow_canary": POST_APPROVAL_PENDING,
            "enforce_eligibility": POST_APPROVAL_PENDING,
        },
        "superseded_invalid_windows": list(superseded_invalid_windows or []),
        "teardown_proof": teardown_proof
        or {
            "guard_mode": "off",
            "teardown_command": shadow_probe.teardown_command(),
            "verified_at_utc": None,
            "note": "preprod_signoff_does_not_require_shadow_teardown",
        },
        "signed_at_utc": _utc_now(),
    }


def sign_bundle(bundle: Mapping[str, Any], *, hmac_key: str) -> dict[str, Any]:
    if not hmac_key:
        raise ValueError("hmac_key_missing")
    signed = dict(bundle)
    signed.pop("signature", None)
    signature = hmac.new(hmac_key.encode("utf-8"), _canonical(signed).encode("utf-8"), hashlib.sha256).hexdigest()
    signed["signature"] = f"hmac-sha256:{signature}"
    return signed


def verify_bundle_signature(bundle: Mapping[str, Any], *, hmac_key: str) -> bool:
    if not hmac_key:
        return False
    payload = dict(bundle)
    signature = str(payload.pop("signature", ""))
    if not signature.startswith("hmac-sha256:"):
        return False
    expected = f"hmac-sha256:{hmac.new(hmac_key.encode('utf-8'), _canonical(payload).encode('utf-8'), hashlib.sha256).hexdigest()}"
    return hmac.compare_digest(signature, expected)


def verify_preprod_signoff_v2_bundle(
    bundle: Mapping[str, Any],
    *,
    hmac_key: str,
    expected_identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []

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
    if not verify_bundle_signature(bundle, hmac_key=hmac_key):
        blockers.append(CODE_BUNDLE_SIGNATURE_INVALID)

    identity = bundle.get("identity_binding")
    blockers.extend(validate_identity_binding(identity if isinstance(identity, Mapping) else {}))
    if expected_identity:
        for key, value in expected_identity.items():
            if str((identity or {}).get(key) or "") != str(value):
                blockers.append(CODE_IDENTITY_BINDING_MISMATCH)

    lifecycle_rows = bundle.get("lifecycle_phases")
    if not isinstance(lifecycle_rows, list):
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
        lifecycle_rows = []
    seen_phases: set[str] = set()
    parsed_rows: list[Mapping[str, Any]] = []
    for row in lifecycle_rows:
        if not isinstance(row, Mapping):
            blockers.append(CODE_LIFECYCLE_PHASE_FAILED)
            continue
        seen_phases.add(str(row.get("phase") or ""))
        blockers.extend(validate_lifecycle_phase_row(row))
        parsed_rows.append(row)
    if seen_phases != set(LIFECYCLE_PHASES):
        blockers.append(CODE_LIFECYCLE_PHASE_MISSING)
    blockers.extend(_validate_stable_counters_across_phases(parsed_rows))

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

    post_approval = bundle.get("post_approval")
    if not isinstance(post_approval, Mapping):
        blockers.append(CODE_POST_APPROVAL_NOT_PENDING)
    else:
        if post_approval.get("canonical_shadow_canary") != POST_APPROVAL_PENDING:
            blockers.append(CODE_POST_APPROVAL_NOT_PENDING)
        if post_approval.get("enforce_eligibility") != POST_APPROVAL_PENDING:
            blockers.append(CODE_POST_APPROVAL_NOT_PENDING)

    superseded = bundle.get("superseded_invalid_windows")
    if isinstance(superseded, list):
        for row in superseded:
            if isinstance(row, Mapping) and row.get("active") is True:
                blockers.append(CODE_SUPERSEDED_WINDOW_ACTIVE)

    teardown = bundle.get("teardown_proof")
    if not isinstance(teardown, Mapping):
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


def read_legacy_v1_bundle(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    readable = is_legacy_v1_bundle(payload)
    return _report(
        PHASE_LEGACY_V1_READ,
        ok=readable,
        code=None if readable else CODE_BUNDLE_INVALID,
        legacy_schema_version=LEGACY_V1_SCHEMA_VERSION if readable else None,
        sufficient_for_preprod=False,
        note="v1 evidence remains historically readable but cannot unlock preprod gates",
        artifact_summary={
            "traffic_claim": payload.get("observation_window", {}).get("traffic_claim")
            if isinstance(payload.get("observation_window"), Mapping)
            else None,
            "status": payload.get("status"),
        }
        if readable
        else None,
    )


def load_and_verify_artifact_from_env(
    *,
    artifact_env: str = SIGNOFF_ARTIFACT_ENV,
    hmac_key_env: str = SIGNOFF_HMAC_KEY_ENV,
    expected_identity: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
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
    payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    return verify_preprod_signoff_v2_bundle(payload, hmac_key=hmac_key, expected_identity=expected_identity)


def execute_full_probe(
    *,
    app_root: Path | None = None,
    pinned_target_revision: str = "a8487b25",
    deployment_id: str = "cbe93c7b-5891-49de-8bd0-5588acad14b5",
    image_digest: str | None = None,
    hmac_key: str | None = None,
) -> dict[str, Any]:
    root = resolve_app_root(app_root)
    manifest = shadow_probe.build_runtime_artifact_manifest(app_root=root)
    identity = build_identity_binding(
        pinned_target_revision=pinned_target_revision,
        manifest_digest=manifest["manifest_digest"],
        deployment_id=deployment_id,
        image_digest=image_digest,
    )

    lifecycle_rows: list[dict[str, Any]] = []
    for phase in LIFECYCLE_PHASES:
        lifecycle_rows.append(execute_lifecycle_matrix_phase(phase=phase, app_root=root))
    negative = execute_negative_controls(app_root=root)

    lifecycle_ok = all(row.get("ok") for row in lifecycle_rows)
    counters_ok = not _validate_stable_counters_across_phases(lifecycle_rows)
    unsigned = build_unsigned_bundle(
        identity_binding=identity,
        lifecycle_phases=lifecycle_rows,
        negative_controls=negative,
        superseded_invalid_windows=[
            {
                "window_id": "arch001-48h-zero-traffic-v1",
                "reason": "superseded_by_preprod_synthetic_signoff_v2",
                "active": False,
                "superseded_at_utc": _utc_now(),
            }
        ],
    )
    signed = sign_bundle(unsigned, hmac_key=hmac_key or "test-hmac-key-for-ci-only")
    verify = verify_preprod_signoff_v2_bundle(signed, hmac_key=hmac_key or "test-hmac-key-for-ci-only")

    return _report(
        PHASE_BUNDLE,
        ok=lifecycle_ok and negative.get("ok") is True and counters_ok and verify.get("ok") is True,
        identity_binding=identity,
        lifecycle_phases=lifecycle_rows,
        negative_controls=negative,
        bundle=signed,
        verify=verify,
        traffic_claim=TRAFFIC_CLAIM,
        post_approval_shadow_canary=POST_APPROVAL_PENDING,
        enforce_eligibility=POST_APPROVAL_PENDING,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments[:1] == ["lifecycle-phase"] and len(arguments) == 2:
            _emit(execute_lifecycle_matrix_phase(phase=arguments[1], app_root=_REPO))
            return 0
        if arguments == ["negative-controls"]:
            _emit(execute_negative_controls(app_root=_REPO))
            return 0
        if arguments == ["full-probe"]:
            result = execute_full_probe(app_root=_REPO)
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments[:1] == ["verify-bundle"] and len(arguments) == 2:
            hmac_key = (os.environ.get(SIGNOFF_HMAC_KEY_ENV) or "").strip()
            payload = json.loads(Path(arguments[1]).read_text(encoding="utf-8"))
            result = verify_preprod_signoff_v2_bundle(payload, hmac_key=hmac_key)
            _emit(result)
            return 0 if result.get("ok") else 1
        if arguments[:1] == ["verify-legacy-v1"] and len(arguments) == 2:
            _emit(read_legacy_v1_bundle(Path(arguments[1])))
            return 0
        if arguments == ["verify-artifact-env"]:
            result = load_and_verify_artifact_from_env()
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
