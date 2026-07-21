"""Regression tests for ARCH-001 preprod synthetic signoff v2 operator/gates."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.tests._arch001_signoff_v2_fixture import (  # noqa: E402
    _PROD_HMAC_KEY,
    _REDEPLOY_DEPLOY,
    install_production_v2_artifact,
    write_production_fixture_tree,
)
from scripts.operators import (  # noqa: E402
    product_availability_preprod_synthetic_signoff_v2 as signoff_v2,
)
from scripts.operators import real_channel_conversational_acceptance as rca_operator
from scripts.operators import staging_acceptance_config_consolidation as consolidation
from scripts.operators.product_availability_preprod_synthetic_signoff_v2_contract import (  # noqa: E402
    BUNDLE_SCHEMA_VERSION,
    CODE_ARTIFACT_UNREADABLE,
    CODE_BUNDLE_SIGNATURE_INVALID,
    CODE_EVIDENCE_CLASS_INELIGIBLE,
    CODE_EXPECTED_IDENTITY_MISSING,
    CODE_HMAC_KEY_WEAK,
    CODE_IDENTITY_BINDING_MISMATCH,
    CODE_LEGACY_V1_NOT_SUFFICIENT,
    CODE_LIFECYCLE_PHASE_FAILED,
    CODE_LIFECYCLE_PHASE_MISSING,
    CODE_MATRIX_CASE_MISSING,
    CODE_MATRIX_INVARIANT_VIOLATION,
    CODE_PHASE_DEPLOYMENT_ID_INVALID,
    CODE_PHASE_TIMESTAMP_ORDER_INVALID,
    CODE_STABLE_COUNTERS_DRIFT,
    CODE_TEARDOWN_PROOF_UNVERIFIED,
    EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST,
    EVIDENCE_CLASS_PRODUCTION_SIGNOFF,
    EXPECTED_MANIFEST_DIGEST_ENV,
    EXPECTED_STABLE_COUNTERS,
    INITIATIVE_ID,
    ISOLATED_DEPLOYMENT_ID_ENV,
    ISOLATED_SERVICE_ID_ENV,
    ISOLATED_SERVICE_NAME_ENV,
    LIFECYCLE_PHASES,
    PHASE_BASELINE,
    PINNED_REVISION_ENV,
    SIGNOFF_ARTIFACT_ENV,
    SIGNOFF_HMAC_KEY_ENV,
    TRAFFIC_CLAIM,
    validate_lifecycle_phase_row,
    validate_phase_artifact,
    validate_stable_counters,
)


def test_validate_lifecycle_phase_row_accumulates_all_blockers() -> None:
    row = {
        "phase": PHASE_BASELINE,
        "ok": False,
        "execution_mode": "external_runner",
        "target_app_root": "/tmp",
        "matrix": {"ok": False, "case_results": [], "guards": {}},
        "stable_counters": {"evaluated_turns": 0},
        "identity_binding": {},
        "isolated_service_constraints": {"no_domains": False, "no_provider_credentials": False},
        "dependency_fault": {"status": "broken"},
    }
    blockers = validate_lifecycle_phase_row(row)
    assert CODE_LIFECYCLE_PHASE_FAILED in blockers
    assert CODE_MATRIX_INVARIANT_VIOLATION in blockers
    assert CODE_STABLE_COUNTERS_DRIFT in blockers
    assert CODE_IDENTITY_BINDING_MISMATCH in blockers


def test_validate_phase_artifact_requires_runtime_binding(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    artifact = json.loads((tmp_path / "phases" / f"{PHASE_BASELINE}.json").read_text(encoding="utf-8"))
    artifact["execution_mode"] = "external_runner"
    blockers = validate_phase_artifact(artifact, expected_identity=fixture["identity"])
    assert CODE_MATRIX_INVARIANT_VIOLATION in blockers or "phase_artifact_invalid" in blockers


def test_contract_self_test_produces_ineligible_bundle() -> None:
    result = signoff_v2.execute_contract_self_test(app_root=_REPO)
    assert result["ok"] is True
    bundle = result["bundle"]
    assert bundle["evidence_class"] == EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST
    assert bundle["eligible_for_signoff"] is False
    gate_reject = result["gate_reject"]
    assert gate_reject["ok"] is False
    assert CODE_EVIDENCE_CLASS_INELIGIBLE in gate_reject["blockers"]


def test_production_bundle_assembly_and_verification(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = fixture["bundle"]
    assert bundle["evidence_class"] == EVIDENCE_CLASS_PRODUCTION_SIGNOFF
    assert bundle["eligible_for_signoff"] is True
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        bundle,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
        require_production_class=True,
    )
    assert verify["ok"] is True


def test_absolute_stable_counters_enforced(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    bundle["lifecycle_phases"][0]["stable_counters"]["would_rewrite_count"] = 99
    signed = signoff_v2.sign_bundle(bundle, hmac_key=fixture["hmac_key"])
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert verify["ok"] is False
    assert CODE_STABLE_COUNTERS_DRIFT in verify["blockers"]


def test_missing_matrix_case_blocks(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    bundle["lifecycle_phases"][0]["matrix"]["case_results"] = bundle["lifecycle_phases"][0]["matrix"]["case_results"][:-1]
    signed = signoff_v2.sign_bundle(bundle, hmac_key=fixture["hmac_key"])
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert CODE_MATRIX_CASE_MISSING in verify["blockers"]


def test_fresh_redeploy_requires_new_deployment_id(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    for row in bundle["lifecycle_phases"]:
        if row.get("phase") == "fresh_pinned_redeploy":
            row["lifecycle_attestation"]["new_deployment_id"] = row["lifecycle_attestation"]["prior_deployment_id"]
    signed = signoff_v2.sign_bundle(bundle, hmac_key=fixture["hmac_key"])
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert CODE_PHASE_DEPLOYMENT_ID_INVALID in verify["blockers"]


def test_repeat_matrix_spacing_enforced(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    repeat = [row for row in bundle["lifecycle_phases"] if str(row.get("phase", "")).startswith("repeat_matrix_")]
    repeat[1]["executed_at_utc"] = repeat[0]["executed_at_utc"]
    signed = signoff_v2.sign_bundle(bundle, hmac_key=fixture["hmac_key"])
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert CODE_PHASE_TIMESTAMP_ORDER_INVALID in verify["blockers"]


def test_weak_hmac_key_rejected(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    with pytest.raises(ValueError, match=CODE_HMAC_KEY_WEAK):
        signoff_v2.sign_bundle(fixture["bundle"], hmac_key="short")


def test_stale_revision_rejected_by_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = install_production_v2_artifact(monkeypatch, tmp_path)
    monkeypatch.setenv(PINNED_REVISION_ENV, "deadbeef")
    result = signoff_v2.verify_arch001_preprod_signoff_for_gate()
    assert result["ok"] is False
    assert CODE_IDENTITY_BINDING_MISMATCH in result["blockers"]


def test_wrong_service_role_rejected_by_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = install_production_v2_artifact(monkeypatch, tmp_path)
    monkeypatch.setenv(ISOLATED_SERVICE_NAME_ENV, "nahla-saas")
    result = signoff_v2.verify_arch001_preprod_signoff_for_gate()
    assert result["ok"] is False


def test_missing_expected_identity_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    monkeypatch.setenv(SIGNOFF_ARTIFACT_ENV, str(fixture["artifact_path"]))
    monkeypatch.setenv(SIGNOFF_HMAC_KEY_ENV, fixture["hmac_key"])
    monkeypatch.delenv(EXPECTED_MANIFEST_DIGEST_ENV, raising=False)
    result = signoff_v2.verify_arch001_preprod_signoff_for_gate()
    assert result["ok"] is False
    assert CODE_EXPECTED_IDENTITY_MISSING in result["code"] or "manifest_digest_missing" in result["blockers"]


def test_malformed_json_fail_closed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv(SIGNOFF_ARTIFACT_ENV, str(bad))
    monkeypatch.setenv(SIGNOFF_HMAC_KEY_ENV, _PROD_HMAC_KEY)
    monkeypatch.setenv(PINNED_REVISION_ENV, "a8487b25")
    monkeypatch.setenv(EXPECTED_MANIFEST_DIGEST_ENV, "0" * 64)
    monkeypatch.setenv(ISOLATED_SERVICE_NAME_ENV, "nahla-arch001-shadow")
    monkeypatch.setenv(ISOLATED_SERVICE_ID_ENV, "22222222-2222-4222-8222-222222222222")
    monkeypatch.setenv(ISOLATED_DEPLOYMENT_ID_ENV, _REDEPLOY_DEPLOY)
    result = signoff_v2.verify_arch001_preprod_signoff_for_gate()
    monkeypatch.undo()
    assert result["ok"] is False
    assert CODE_ARTIFACT_UNREADABLE in result["blockers"]


def test_real_channel_gate_binds_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_production_v2_artifact(monkeypatch, tmp_path)
    assert rca_operator.gate_arch001_shadow_signoff()["ok"] is True
    monkeypatch.setenv(ISOLATED_DEPLOYMENT_ID_ENV, "00000000-0000-4000-8000-000000000000")
    assert rca_operator.gate_arch001_shadow_signoff()["ok"] is False


def test_staging_teardown_gate_binds_identity(tmp_path: Path) -> None:
    env = write_production_fixture_tree(tmp_path)
    env_map = {
        SIGNOFF_ARTIFACT_ENV: str(env["artifact_path"]),
        SIGNOFF_HMAC_KEY_ENV: env["hmac_key"],
        PINNED_REVISION_ENV: env["identity"]["pinned_target_revision"],
        EXPECTED_MANIFEST_DIGEST_ENV: env["manifest_digest"],
        ISOLATED_SERVICE_NAME_ENV: env["identity"]["service_name"],
        ISOLATED_SERVICE_ID_ENV: env["identity"]["service_id"],
        ISOLATED_DEPLOYMENT_ID_ENV: env["identity"]["deployment_id"],
        "NAHLA_ARCH001_SHADOW_TEARDOWN_PROOF": "teardown-ref",
    }
    assert consolidation.gate_arch001_teardown_proof(env=env_map)["ok"] is True
    env_map[ISOLATED_SERVICE_ID_ENV] = "00000000-0000-4000-8000-000000000000"
    assert consolidation.gate_arch001_teardown_proof(env=env_map)["ok"] is False


def test_teardown_placeholder_rejected(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    bundle["teardown_proof"]["isolated_service"]["verified_at_utc"] = None
    signed = signoff_v2.sign_bundle(bundle, hmac_key=fixture["hmac_key"])
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        signed,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert CODE_TEARDOWN_PROOF_UNVERIFIED in verify["blockers"]


def test_legacy_v1_readable_but_ineligible(tmp_path: Path) -> None:
    legacy = _REPO / "docs/engineering/staging-evidence/product-availability-shadow-baseline-2026-07-18.json"
    if not legacy.is_file():
        pytest.skip("legacy v1 fixture not present")
    assert signoff_v2.read_legacy_v1_bundle(legacy)["sufficient_for_preprod"] is False
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(payload, hmac_key=_PROD_HMAC_KEY)
    assert CODE_LEGACY_V1_NOT_SUFFICIENT in verify["blockers"]


def test_expected_stable_counter_constants_match_matrix(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    counters = fixture["bundle"]["lifecycle_phases"][0]["stable_counters"]
    assert validate_stable_counters(counters) == []
    assert counters == EXPECTED_STABLE_COUNTERS


def test_cli_contract_self_test_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.product_availability_preprod_synthetic_signoff_v2", "contract-self-test"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["evidence_class"] == EVIDENCE_CLASS_CI_CONTRACT_SELF_TEST


def test_signature_tamper_fails_closed(tmp_path: Path) -> None:
    fixture = write_production_fixture_tree(tmp_path)
    bundle = copy.deepcopy(fixture["bundle"])
    bundle["traffic_claim"] = "organic_traffic_observed"
    verify = signoff_v2.verify_preprod_signoff_v2_bundle(
        bundle,
        hmac_key=fixture["hmac_key"],
        expected_identity=fixture["identity"],
    )
    assert CODE_BUNDLE_SIGNATURE_INVALID in verify["blockers"]
