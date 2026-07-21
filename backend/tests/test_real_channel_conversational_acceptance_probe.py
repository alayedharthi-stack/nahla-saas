"""Regression tests for real-channel conversational acceptance operator (default-off)."""
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
    real_channel_conversational_acceptance as operator,
)
from backend.tests._arch001_signoff_v2_fixture import install_valid_v2_artifact
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_MANIFEST_INVALID,
    CODE_TENANT_NOT_ALLOWED,
    EXECUTION_CONFIRM_ENV,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MANIFEST_SCHEMA_VERSION,
    MASTER_ENABLE_ENV,
    PHASE_DEFAULT_OFF,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    PHASE_TENANT_48_SALLA_MINIMAL,
    REPORT_SCHEMA_VERSION,
    SCENARIO_TAXONOMY,
    TENANT_1_PASS_CONFIRM_ENV,
    TENANT_48_SALLA_MINIMAL,
    load_scenario_manifest,
    resolve_acceptance_phase,
    validate_manifest,
)
from scripts.operators.real_channel_acceptance_manifest_builder import (  # noqa: E402
    build_manifest,
)


def test_default_off_probe_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = operator.execute_default_off_probe()
    assert result["phase"] == PHASE_DEFAULT_OFF
    assert result["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["acceptance_enabled"] is False


def test_manifest_covers_closed_taxonomy() -> None:
    manifest = load_scenario_manifest(_REPO)
    assert manifest["manifest_schema_version"] == MANIFEST_SCHEMA_VERSION
    taxonomies = {row["taxonomy"] for row in manifest["scenarios"]}
    assert taxonomies == set(SCENARIO_TAXONOMY)


def test_manifest_scenario_counts() -> None:
    manifest = load_scenario_manifest(_REPO)
    t1 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_1_INTENSIVE]
    t33 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_33_LIMITED]
    t48 = [s for s in manifest["scenarios"] if s["phase"] == PHASE_TENANT_48_SALLA_MINIMAL]
    assert len(t1) == 49
    assert len(t33) == 16
    assert len(t48) == 16
    assert manifest["scenario_count"] == 81
    assert manifest["phase_scenario_counts"] == {
        PHASE_TENANT_1_INTENSIVE: 49,
        PHASE_TENANT_33_LIMITED: 16,
        PHASE_TENANT_48_SALLA_MINIMAL: 16,
    }


def test_tenant_48_phase_independent_of_tenant_1_pass() -> None:
    manifest = load_scenario_manifest(_REPO)
    phase_row = next(
        row for row in manifest["phases"] if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL
    )
    assert phase_row["tenant_id"] == TENANT_48_SALLA_MINIMAL
    assert phase_row["requires_tenant_1_pass"] is False
    assert phase_row["independent_of_tenant_1_pass_artifact"] is True
    for row in manifest["scenarios"]:
        if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL:
            assert row["tenant_id"] == TENANT_48_SALLA_MINIMAL
            assert row["preconditions"]["tenant_1_pass_required"] is False
            assert row["scenario_id"].startswith("t48_")


def test_resolve_acceptance_phase_rejects_arbitrary_tenant() -> None:
    with pytest.raises(ValueError, match=CODE_TENANT_NOT_ALLOWED):
        resolve_acceptance_phase(7)


def test_manifest_rejects_arbitrary_tenant_id() -> None:
    manifest = build_manifest()
    manifest["scenarios"] = list(manifest["scenarios"])
    manifest["scenarios"][0] = dict(manifest["scenarios"][0])
    manifest["scenarios"][0]["tenant_id"] = 99
    with pytest.raises(ValueError, match=CODE_MANIFEST_INVALID):
        validate_manifest(manifest)


def test_tenant_48_session_start_still_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.operators.real_channel_acceptance_session import start_session

    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = start_session(tenant_id=TENANT_48_SALLA_MINIMAL, app_root=_REPO)
    assert result["ok"] is False
    assert CODE_ACCEPTANCE_NOT_ENABLED in result["blockers"]


def test_tenant_33_requires_tenant_1_pass_in_preconditions() -> None:
    manifest = load_scenario_manifest(_REPO)
    for row in manifest["scenarios"]:
        if row["phase"] == PHASE_TENANT_33_LIMITED:
            assert row["preconditions"]["store_ai_mode"] == "test"
            assert row["preconditions"]["store_ai_enabled"] is True
            assert row["preconditions"]["arch001_shadow_signoff"] is True
            assert row["preconditions"]["tenant_1_pass_required"] is True
            assert row["preconditions"]["real_catalog_data"] is True
            assert (
                row["preconditions"]["phone_env_ref"]
                == "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE"
            )


def test_tenant_1_generated_preconditions_remain_unchanged() -> None:
    manifest = build_manifest()
    tenant_1_rows = [
        row for row in manifest["scenarios"] if row["phase"] == PHASE_TENANT_1_INTENSIVE
    ]
    assert len(tenant_1_rows) == 49
    for row in tenant_1_rows:
        assert row["preconditions"] == {
            "store_ai_mode": "test",
            "store_ai_enabled": True,
            "store_label": "متجر تجريبي عام",
            "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE",
            "arch001_shadow_signoff": True,
        }


def test_tenant_48_evidence_backed_scenario_contracts() -> None:
    manifest = build_manifest()
    tenant_48 = {
        row["scenario_id"]: row
        for row in manifest["scenarios"]
        if row["phase"] == PHASE_TENANT_48_SALLA_MINIMAL
    }

    saved_address = tenant_48["t48_saved_address_fail_closed"]
    assert saved_address["expected_state"] == {
        "persisted_address_lookup": True,
        "reuse_only_if_verified_persisted_address_exists": True,
        "clarify_or_collect_if_address_missing": True,
    }
    assert "previous_address_claim_without_db_state" in saved_address["prohibited_claims"]

    tracking = tenant_48["t48_tracking_existing"]
    fixture = tracking["preconditions"]["synthetic_acceptance_order_fixture"]
    assert fixture["deterministic_reference"] == "RRRD1234"
    assert fixture["scope"] == "tenant_48_private_test_tenant_only"
    assert fixture["cleanup_required"] is True
    assert "remove_tenant_48_synthetic_acceptance_order_fixture" in tracking["cleanup"]

    no_reference = tenant_48["t48_delivery_no_reference"]
    assert "synthetic_acceptance_order_fixture" not in no_reference["preconditions"]
    assert "delivery_date_without_evidence" in no_reference["prohibited_claims"]

    handoff = tenant_48["t48_handoff"]
    assert handoff["expected_state"] == {
        "staff_handoff_evidence_required": True,
        "ai_continuity": True,
        "explicit_pause_state_respected": True,
        "resume_only_after_state_transition": True,
    }

    timeout = tenant_48["t48_tool_timeout"]
    fault = timeout["preconditions"]["controlled_staging_fault_injection"]
    assert fault["scope"] == "tenant_48_tool_timeout_only"
    assert fault["cleanup_restore_required"] is True
    assert fault["production_fault_injector_added_by_this_pr"] is False
    assert "success_claim_after_tool_timeout" in timeout["prohibited_claims"]
    assert "disable_controlled_staging_fault_injection" in timeout["cleanup"]


def test_all_scenarios_require_real_channel_path() -> None:
    manifest = load_scenario_manifest(_REPO)
    for row in manifest["scenarios"]:
        assert row["execution_path"] == EXECUTION_PATH_REAL_CHANNEL_WEBHOOK


def test_scenario_preconditions_require_test_mode() -> None:
    manifest = build_manifest()
    validate_manifest(manifest)
    for row in manifest["scenarios"]:
        assert row["preconditions"]["store_ai_mode"] == "test"


def test_execution_blocked_without_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_ENABLE_ENV, raising=False)
    result = operator.execute_scenario_plan(
        phase=PHASE_TENANT_1_INTENSIVE, app_root=_REPO, dry_run=False
    )
    assert result["ok"] is False
    assert result["code"] == "real_channel_required"


def test_tenant_33_blocked_without_tenant_1_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(EXECUTION_CONFIRM_ENV, "true")
    install_valid_v2_artifact(monkeypatch, tmp_path)
    monkeypatch.delenv(TENANT_1_PASS_CONFIRM_ENV, raising=False)
    result = operator.execute_scenario_plan(
        phase=PHASE_TENANT_33_LIMITED, app_root=_REPO, dry_run=False
    )
    assert result["ok"] is False
    assert result["code"] == "real_channel_required"


def test_arch001_signoff_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT", raising=False)
    monkeypatch.delenv("NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY", raising=False)
    result = operator.gate_arch001_shadow_signoff()
    assert result["ok"] is False
    assert result["code"] in {CODE_ARCH001_SIGNOFF_MISSING, "bundle_invalid"}


def test_defect_bundle_template_writes_sanitized_file(tmp_path: Path) -> None:
    bundle_root = tmp_path / "nahla"
    (bundle_root / "backend").mkdir(parents=True)
    result = operator.build_defect_bundle(
        scenario_id="t1_faq_hours",
        failure_class="prohibited_operational_claim",
        correlation_id="corr-test-1234",
        sanitized_evidence={"compose_source": "llm", "phone_hash": "sha256:abc"},
        app_root=bundle_root,
    )
    assert result["ok"] is True
    bundle_rel = result["bundle_path"]
    bundle = json.loads((bundle_root / bundle_rel).read_text(encoding="utf-8"))
    assert bundle["classification"]["auto_merge_fixes"] is False
    assert "966" not in json.dumps(bundle)


def test_cli_default_off_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.real_channel_conversational_acceptance", "default-off"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True


def test_cli_manifest_validate_exit_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.real_channel_conversational_acceptance", "manifest-validate"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
