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
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    EXECUTION_CONFIRM_ENV,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MANIFEST_SCHEMA_VERSION,
    MASTER_ENABLE_ENV,
    PHASE_DEFAULT_OFF,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    REPORT_SCHEMA_VERSION,
    SCENARIO_TAXONOMY,
    TENANT_1_PASS_CONFIRM_ENV,
    load_scenario_manifest,
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
    assert len(t1) == 49
    assert len(t33) == 16
    assert manifest["scenario_count"] == 65


def test_tenant_33_requires_tenant_1_pass_in_preconditions() -> None:
    manifest = load_scenario_manifest(_REPO)
    for row in manifest["scenarios"]:
        if row["phase"] == PHASE_TENANT_33_LIMITED:
            assert row["preconditions"]["tenant_1_pass_required"] is True
            assert row["preconditions"]["real_catalog_data"] is True


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
    assert result["code"] == CODE_ACCEPTANCE_NOT_ENABLED


def test_tenant_33_blocked_without_tenant_1_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_ENABLE_ENV, "true")
    monkeypatch.setenv(EXECUTION_CONFIRM_ENV, "true")
    monkeypatch.setenv(ARCH001_SHADOW_SIGNOFF_ENV, "true")
    monkeypatch.delenv(TENANT_1_PASS_CONFIRM_ENV, raising=False)
    result = operator.execute_scenario_plan(
        phase=PHASE_TENANT_33_LIMITED, app_root=_REPO, dry_run=False
    )
    assert result["ok"] is False
    assert result["code"] == "tenant_1_not_passed"


def test_arch001_signoff_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ARCH001_SHADOW_SIGNOFF_ENV, raising=False)
    result = operator.gate_arch001_shadow_signoff()
    assert result["ok"] is False
    assert result["code"] == CODE_ARCH001_SIGNOFF_MISSING


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
