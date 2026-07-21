"""Test helpers for ARCH-001 preprod signoff v2 production-class fixtures."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.operators import product_availability_preprod_synthetic_signoff_v2 as signoff_v2
from scripts.operators import product_availability_truth_guard_shadow_observation as shadow_probe
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (
    BASELINE_IMAGE_DIGEST_ENV,
    CANONICAL_DEPLOYMENT_ID_ENV,
    CANONICAL_SERVICE_ID_ENV,
    CANONICAL_SERVICE_NAME_ENV,
    DEPLOYMENT_APP_ROOT,
    EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
    EXECUTION_MODE_IN_CONTAINER,
    EXPECTED_IMAGE_DIGEST_ENV,
    EXPECTED_MANIFEST_DIGEST_ENV,
    ISOLATED_DEPLOYMENT_ID_ENV,
    ISOLATED_SERVICE_ID_ENV,
    ISOLATED_SERVICE_NAME_ENV,
    LIFECYCLE_PHASES,
    NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION,
    NEGATIVE_CONTROL_EXPECTED_CODES,
    NEGATIVE_CONTROL_IDS,
    PHASE_ARTIFACT_SCHEMA_VERSION,
    PHASE_BASELINE,
    PHASE_CONTAINER_RESTART,
    PHASE_FRESH_PINNED_REDEPLOY,
    PINNED_REVISION_ENV,
    REQUIRED_SUPERSEDED_WINDOW_IDS,
    SERVICE_ROLE_CANONICAL_CONTROL,
    SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
    TEARDOWN_PROOF_SCHEMA_VERSION,
    extract_stable_counters,
)
from scripts.operators.deployment_revision_attestation_contract import read_checkout_revision
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (
    SHADOW_MODE_ENV,
    SHADOW_MODE_VALUE,
)

_REPO = Path(__file__).resolve().parents[2]
_PROD_HMAC_KEY = "production-test-arch001-preprod-signoff-v2-key-32b"
_BASELINE_DEPLOY = "cbe93c7b-5891-49de-8bd0-5588acad14b5"
_REDEPLOY_DEPLOY = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_ISOLATED_SERVICE = "nahla-arch001-shadow"
_ISOLATED_SERVICE_ID = "22222222-2222-4222-8222-222222222222"
_CANONICAL_SERVICE = "nahla-saas"
_CANONICAL_SERVICE_ID = "686b36c5-a926-4e58-912a-5e9d13fbc2e7"
_CANONICAL_DEPLOYMENT_ID = "33333333-3333-4333-8333-333333333333"
_BASELINE_IMAGE = "a" * 64
_REDEPLOY_IMAGE = "b" * 64


def _pinned_revision(app_root: Path) -> str:
    checkout = read_checkout_revision(app_root)
    if not checkout:
        return "a8487b25" + ("0" * 32)
    return checkout[:40].lower()


def _matrix(app_root: Path) -> dict[str, Any]:
    import os

    os.environ[SHADOW_MODE_ENV] = SHADOW_MODE_VALUE
    try:
        return shadow_probe.execute_synthetic_matrix_probe(app_root=app_root)
    finally:
        os.environ.pop(SHADOW_MODE_ENV, None)


def _identity(*, manifest_digest: str, pinned_revision: str, deployment_id: str = _REDEPLOY_DEPLOY, image_digest: str = _REDEPLOY_IMAGE) -> dict[str, str]:
    return {
        "pinned_target_revision": pinned_revision,
        "manifest_digest": manifest_digest,
        "service_role": SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
        "service_name": _ISOLATED_SERVICE,
        "service_id": _ISOLATED_SERVICE_ID,
        "deployment_id": deployment_id,
        "image_digest": image_digest,
    }


def _phase_artifact(
    *,
    phase: str,
    start: datetime,
    offset_minutes: int,
    identity: dict[str, str],
    matrix: dict[str, Any],
    baseline_deployment_id: str,
    redeploy_deployment_id: str,
    baseline_image_digest: str,
    redeploy_image_digest: str,
) -> dict[str, Any]:
    executed_at = (start + timedelta(minutes=offset_minutes)).replace(microsecond=0).isoformat()
    binding = dict(identity)
    if phase == PHASE_BASELINE:
        binding["deployment_id"] = baseline_deployment_id
        binding["image_digest"] = baseline_image_digest
        attestation = {"phase": phase, "action": "initial_deploy"}
    elif phase == PHASE_CONTAINER_RESTART:
        binding["deployment_id"] = baseline_deployment_id
        binding["image_digest"] = baseline_image_digest
        attestation = {
            "phase": phase,
            "action": "container_restart",
            "restart_evidence": {
                "prior_container_id": "prior-container-1",
                "new_container_id": "new-container-1",
                "restart_completed_at_utc": executed_at,
            },
        }
    elif phase == PHASE_FRESH_PINNED_REDEPLOY:
        binding["deployment_id"] = redeploy_deployment_id
        binding["image_digest"] = redeploy_image_digest
        attestation = {
            "phase": phase,
            "action": "fresh_pinned_redeploy",
            "prior_deployment_id": baseline_deployment_id,
            "new_deployment_id": redeploy_deployment_id,
        }
    else:
        binding["deployment_id"] = redeploy_deployment_id
        binding["image_digest"] = redeploy_image_digest
        seq = int(phase.rsplit("_", 1)[-1])
        attestation = {
            "phase": phase,
            "action": "repeat_matrix",
            "sequence": seq,
            "deployment_id": redeploy_deployment_id,
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
    }


def _superseded_windows_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "windows": [
            {
                "window_id": "arch001-48h-zero-traffic-20260718",
                "reason": "superseded_by_preprod_synthetic_signoff_v2",
                "active": False,
                "superseded_at_utc": now,
            },
            {
                "window_id": "arch001-48h-zero-traffic-20260720",
                "reason": "superseded_by_preprod_synthetic_signoff_v2",
                "active": False,
                "superseded_at_utc": now,
            },
        ]
    }


def write_production_fixture_tree(tmp_path: Path, *, app_root: Path = _REPO) -> dict[str, Any]:
    manifest = shadow_probe.build_runtime_artifact_manifest(app_root=app_root)
    pinned_revision = _pinned_revision(app_root)
    matrix = _matrix(app_root)
    identity = _identity(manifest_digest=manifest["manifest_digest"], pinned_revision=pinned_revision)
    start = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    offsets = [0, 5, 10, 30, 45, 60]

    phase_dir = tmp_path / "phases"
    phase_dir.mkdir()
    for phase, offset in zip(LIFECYCLE_PHASES, offsets):
        artifact = _phase_artifact(
            phase=phase,
            start=start,
            offset_minutes=offset,
            identity=identity,
            matrix=matrix,
            baseline_deployment_id=_BASELINE_DEPLOY,
            redeploy_deployment_id=_REDEPLOY_DEPLOY,
            baseline_image_digest=_BASELINE_IMAGE,
            redeploy_image_digest=_REDEPLOY_IMAGE,
        )
        (phase_dir / f"{phase}.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    negative_dir = tmp_path / "negative_controls"
    negative_dir.mkdir()
    control_executed_at = (start + timedelta(minutes=65)).replace(microsecond=0).isoformat()
    for control_id in sorted(NEGATIVE_CONTROL_IDS):
        (negative_dir / f"{control_id}.json").write_text(
            json.dumps(
                {
                    "negative_control_schema_version": NEGATIVE_CONTROL_ARTIFACT_SCHEMA_VERSION,
                    "control_id": control_id,
                    "blocked": True,
                    "code": NEGATIVE_CONTROL_EXPECTED_CODES[control_id],
                    "executed_at_utc": control_executed_at,
                    "execution_mode": EXECUTION_MODE_IN_CONTAINER,
                    "target_app_root": DEPLOYMENT_APP_ROOT,
                    "identity_binding": identity,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    teardown_verified_at = (start + timedelta(minutes=70)).replace(microsecond=0).isoformat()
    teardown = {
        "teardown_proof_schema_version": TEARDOWN_PROOF_SCHEMA_VERSION,
        "isolated_service": {
            "guard_mode": "off",
            "service_state": "stopped",
            "verified_at_utc": teardown_verified_at,
            "service_role": SERVICE_ROLE_ISOLATED_PREPROD_SHADOW,
            "service_name": _ISOLATED_SERVICE,
            "service_id": _ISOLATED_SERVICE_ID,
            "deployment_id": _REDEPLOY_DEPLOY,
        },
        "canonical_control": {
            "guard_mode": "off",
            "service_role": SERVICE_ROLE_CANONICAL_CONTROL,
            "service_name": _CANONICAL_SERVICE,
            "service_id": _CANONICAL_SERVICE_ID,
            "deployment_id": _CANONICAL_DEPLOYMENT_ID,
            "verified_at_utc": teardown_verified_at,
        },
    }
    teardown_path = tmp_path / "teardown.json"
    teardown_path.write_text(json.dumps(teardown, indent=2), encoding="utf-8")

    superseded_path = tmp_path / "superseded_windows.json"
    superseded_path.write_text(json.dumps(_superseded_windows_payload(), indent=2), encoding="utf-8")

    assembled = signoff_v2.assemble_bundle_from_artifacts(
        phase_dir=phase_dir,
        teardown_path=teardown_path,
        negative_controls_dir=negative_dir,
        superseded_windows_path=superseded_path,
        expected_identity=identity,
        expected_canonical={
            "service_role": SERVICE_ROLE_CANONICAL_CONTROL,
            "service_name": _CANONICAL_SERVICE,
            "service_id": _CANONICAL_SERVICE_ID,
            "deployment_id": _CANONICAL_DEPLOYMENT_ID,
        },
        hmac_key=_PROD_HMAC_KEY,
        env={BASELINE_IMAGE_DIGEST_ENV: _BASELINE_IMAGE},
    )
    if assembled.get("ok") is not True:
        raise AssertionError(assembled)
    artifact_path = tmp_path / "arch001-preprod-signoff-v2.json"
    artifact_path.write_text(json.dumps(assembled["bundle"], indent=2), encoding="utf-8")
    return {
        "artifact_path": artifact_path,
        "identity": identity,
        "manifest_digest": manifest["manifest_digest"],
        "hmac_key": _PROD_HMAC_KEY,
        "bundle": assembled["bundle"],
        "phase_dir": phase_dir,
        "teardown_path": teardown_path,
        "negative_controls_dir": negative_dir,
        "superseded_windows_path": superseded_path,
        "canonical_identity": {
            "service_role": SERVICE_ROLE_CANONICAL_CONTROL,
            "service_name": _CANONICAL_SERVICE,
            "service_id": _CANONICAL_SERVICE_ID,
            "deployment_id": _CANONICAL_DEPLOYMENT_ID,
        },
    }


def install_production_v2_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    fixture = write_production_fixture_tree(tmp_path)
    monkeypatch.setenv(SIGNOFF_ARTIFACT_ENV, str(fixture["artifact_path"]))
    monkeypatch.setenv(SIGNOFF_HMAC_KEY_ENV, fixture["hmac_key"])
    monkeypatch.setenv(PINNED_REVISION_ENV, fixture["identity"]["pinned_target_revision"])
    monkeypatch.setenv(EXPECTED_MANIFEST_DIGEST_ENV, fixture["manifest_digest"])
    monkeypatch.setenv(EXPECTED_IMAGE_DIGEST_ENV, fixture["identity"]["image_digest"])
    monkeypatch.setenv(BASELINE_IMAGE_DIGEST_ENV, _BASELINE_IMAGE)
    monkeypatch.setenv(ISOLATED_SERVICE_NAME_ENV, fixture["identity"]["service_name"])
    monkeypatch.setenv(ISOLATED_SERVICE_ID_ENV, fixture["identity"]["service_id"])
    monkeypatch.setenv(ISOLATED_DEPLOYMENT_ID_ENV, fixture["identity"]["deployment_id"])
    monkeypatch.setenv(CANONICAL_SERVICE_NAME_ENV, _CANONICAL_SERVICE)
    monkeypatch.setenv(CANONICAL_SERVICE_ID_ENV, _CANONICAL_SERVICE_ID)
    monkeypatch.setenv(CANONICAL_DEPLOYMENT_ID_ENV, _CANONICAL_DEPLOYMENT_ID)
    monkeypatch.setenv("NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION", fixture["identity"]["pinned_target_revision"])
    return fixture


def v2_env_overlay(tmp_path: Path) -> dict[str, str]:
    fixture = write_production_fixture_tree(tmp_path)
    return {
        SIGNOFF_ARTIFACT_ENV: str(fixture["artifact_path"]),
        SIGNOFF_HMAC_KEY_ENV: fixture["hmac_key"],
        PINNED_REVISION_ENV: fixture["identity"]["pinned_target_revision"],
        EXPECTED_MANIFEST_DIGEST_ENV: fixture["manifest_digest"],
        EXPECTED_IMAGE_DIGEST_ENV: fixture["identity"]["image_digest"],
        BASELINE_IMAGE_DIGEST_ENV: _BASELINE_IMAGE,
        ISOLATED_SERVICE_NAME_ENV: fixture["identity"]["service_name"],
        ISOLATED_SERVICE_ID_ENV: fixture["identity"]["service_id"],
        ISOLATED_DEPLOYMENT_ID_ENV: fixture["identity"]["deployment_id"],
        CANONICAL_SERVICE_NAME_ENV: _CANONICAL_SERVICE,
        CANONICAL_SERVICE_ID_ENV: _CANONICAL_SERVICE_ID,
        CANONICAL_DEPLOYMENT_ID_ENV: _CANONICAL_DEPLOYMENT_ID,
    }
