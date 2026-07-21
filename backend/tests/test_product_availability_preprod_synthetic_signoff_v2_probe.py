"""Regression tests for ARCH-001 preprod synthetic signoff v2 operator/gates."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import (  # noqa: E402
    product_availability_preprod_synthetic_signoff_v2 as signoff_v2,
)
from scripts.operators import real_channel_conversational_acceptance as rca_operator
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (  # noqa: E402
    BUNDLE_SCHEMA_VERSION,
    CODE_BUNDLE_SIGNATURE_INVALID,
    CODE_IDENTITY_BINDING_MISMATCH,
    CODE_LEGACY_V1_NOT_SUFFICIENT,
    CODE_LIFECYCLE_PHASE_MISSING,
    CODE_STABLE_COUNTERS_DRIFT,
    INITIATIVE_ID,
    LIFECYCLE_PHASES,
    NEGATIVE_CONTROL_EXPECTED_CODES,
    PHASE_BASELINE,
    PHASE_NEGATIVE_CONTROLS,
    REQUIRED_CASE_IDS,
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
    TRAFFIC_CLAIM,
)
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (  # noqa: E402
    SHADOW_MODE_ENV,
)

_HMAC_KEY = "test-arch001-preprod-signoff-v2-hmac"
_DEPLOYMENT_ID = "cbe93c7b-5891-49de-8bd0-5588acad14b5"


def _write_valid_bundle(tmp_path: Path, *, hmac_key: str = _HMAC_KEY) -> Path:
    result = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=hmac_key)
    assert result["ok"] is True, result
    artifact = tmp_path / "arch001-preprod-signoff-v2.json"
    artifact.write_text(json.dumps(result["bundle"], indent=2), encoding="utf-8")
    return artifact


def _install_valid_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    artifact = _write_valid_bundle(tmp_path)
    monkeypatch.setenv(SIGNOFF_ARTIFACT_ENV, str(artifact))
    monkeypatch.setenv(SIGNOFF_HMAC_KEY_ENV, _HMAC_KEY)
    return artifact


def test_lifecycle_phase_runs_seven_case_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SHADOW_MODE_ENV, "shadow")
    result = signoff_v2.execute_lifecycle_matrix_phase(phase=PHASE_BASELINE, app_root=_REPO)
    assert result["ok"] is True
    case_ids = {row["case_id"] for row in result["matrix"]["case_results"]}
    assert case_ids == REQUIRED_CASE_IDS
    assert result["matrix"]["guards"]["customer_text_changed_count"] == 0
    assert result["matrix"]["guards"]["additional_llm_calls"] == 0
    assert result["matrix"]["guards"]["outbound_provider_calls"] == 0
    assert result["matrix"]["guards"]["duplicate_invocation_count"] == 0


def test_full_probe_includes_all_lifecycle_phases_and_signature(tmp_path: Path) -> None:
    result = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=_HMAC_KEY)
    assert result["ok"] is True
    assert result["traffic_claim"] == TRAFFIC_CLAIM
    assert result["post_approval_shadow_canary"] == "pending"
    assert result["enforce_eligibility"] == "pending"
    bundle = result["bundle"]
    assert bundle["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION
    assert bundle["initiative_id"] == INITIATIVE_ID
    assert bundle["traffic_claim"] == TRAFFIC_CLAIM
    assert bundle["signature"].startswith("hmac-sha256:")
    phases = [row["phase"] for row in bundle["lifecycle_phases"]]
    assert phases == list(LIFECYCLE_PHASES)
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(bundle, hmac_key=_HMAC_KEY)
    assert verify["ok"] is True


def test_missing_lifecycle_phase_fails_closed(tmp_path: Path) -> None:
    bundle = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=_HMAC_KEY)["bundle"]
    bundle["lifecycle_phases"] = bundle["lifecycle_phases"][:-1]
    bundle = signoff_v2.sign_bundle(bundle, hmac_key=_HMAC_KEY)
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(bundle, hmac_key=_HMAC_KEY)
    assert verify["ok"] is False
    assert CODE_LIFECYCLE_PHASE_MISSING in verify["blockers"]


def test_signature_tamper_fails_closed(tmp_path: Path) -> None:
    bundle = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=_HMAC_KEY)["bundle"]
    bundle["traffic_claim"] = "organic_traffic_observed"
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(bundle, hmac_key=_HMAC_KEY)
    assert verify["ok"] is False
    assert CODE_BUNDLE_SIGNATURE_INVALID in verify["blockers"]


def test_identity_binding_mismatch_fails_closed(tmp_path: Path) -> None:
    bundle = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=_HMAC_KEY)["bundle"]
    bundle["identity_binding"]["deployment_id"] = "00000000-0000-4000-8000-000000000000"
    bundle = signoff_v2.sign_bundle(bundle, hmac_key=_HMAC_KEY)
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        bundle,
        hmac_key=_HMAC_KEY,
        expected_identity={"deployment_id": _DEPLOYMENT_ID},
    )
    assert verify["ok"] is False
    assert CODE_IDENTITY_BINDING_MISMATCH in verify["blockers"]


def test_stable_counter_drift_fails_closed(tmp_path: Path) -> None:
    bundle = signoff_v2.execute_full_probe(app_root=_REPO, hmac_key=_HMAC_KEY)["bundle"]
    bundle["lifecycle_phases"][1]["stable_counters"]["would_rewrite_count"] = 999
    bundle = signoff_v2.sign_bundle(bundle, hmac_key=_HMAC_KEY)
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(bundle, hmac_key=_HMAC_KEY)
    assert verify["ok"] is False
    assert CODE_STABLE_COUNTERS_DRIFT in verify["blockers"]


def test_negative_controls_block_expected_codes() -> None:
    result = signoff_v2.execute_negative_controls(app_root=_REPO)
    assert result["ok"] is True
    by_id = {row["control_id"]: row for row in result["controls"]}
    for control_id, expected in NEGATIVE_CONTROL_EXPECTED_CODES.items():
        assert by_id[control_id]["blocked"] is True
        assert by_id[control_id]["code"] == expected


def test_legacy_v1_readable_but_not_sufficient(tmp_path: Path) -> None:
    legacy = _REPO / "docs/engineering/staging-evidence/product-availability-shadow-baseline-2026-07-18.json"
    if not legacy.is_file():
        pytest.skip("legacy v1 fixture not present in workspace")
    read = signoff_v2.read_legacy_v1_bundle(legacy)
    assert read["ok"] is True
    assert read["sufficient_for_preprod"] is False
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(payload, hmac_key=_HMAC_KEY)
    assert verify["ok"] is False
    assert CODE_LEGACY_V1_NOT_SUFFICIENT in verify["blockers"]


def test_real_channel_gate_requires_v2_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(SIGNOFF_ARTIFACT_ENV, raising=False)
    result = rca_operator.gate_arch001_shadow_signoff()
    assert result["ok"] is False
    assert result["code"] is not None

    _install_valid_artifact(monkeypatch, tmp_path)
    result = rca_operator.gate_arch001_shadow_signoff()
    assert result["ok"] is True
    assert result["arch001_preprod_signoff_v2_valid"] is True


def test_env_only_signoff_flag_not_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM", "true")
    monkeypatch.delenv(SIGNOFF_ARTIFACT_ENV, raising=False)
    result = rca_operator.gate_arch001_shadow_signoff()
    assert result["ok"] is False


def test_cli_full_probe_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.product_availability_preprod_synthetic_signoff_v2", "full-probe"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True
