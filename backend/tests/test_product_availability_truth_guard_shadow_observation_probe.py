"""Regression tests for product availability truth guard shadow observation operator."""
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
    product_availability_truth_guard_shadow_observation as probe,
)
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (  # noqa: E402
    CODE_SHADOW_MODE_NOT_ENABLED,
    FIXTURE_TENANT_A,
    FIXTURE_TENANT_B,
    PHASE_DEFAULT_OFF,
    PHASE_SYNTHETIC_MATRIX,
    REPORT_SCHEMA_VERSION,
    SHADOW_MODE_ENV,
)


def test_default_off_probe_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_MODE_ENV, raising=False)
    result = probe.execute_default_off_probe(app_root=_REPO)
    assert result["phase"] == PHASE_DEFAULT_OFF
    assert result["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["guard_mode"] == "off"
    assert result["customer_text_changed"] is False


def test_synthetic_matrix_requires_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_MODE_ENV, raising=False)
    result = probe.execute_synthetic_matrix_probe(app_root=_REPO)
    assert result["ok"] is False
    assert result["code"] == CODE_SHADOW_MODE_NOT_ENABLED


def test_synthetic_matrix_shadow_byte_identical_and_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SHADOW_MODE_ENV, "shadow")
    result = probe.execute_synthetic_matrix_probe(app_root=_REPO)
    assert result["ok"] is True
    assert result["phase"] == PHASE_SYNTHETIC_MATRIX
    assert result["guards"]["outbound_provider_calls"] == 0
    assert result["guards"]["additional_llm_calls"] == 0
    assert result["guards"]["customer_text_changed_count"] == 0
    assert all(row["byte_identical"] for row in result["case_results"])
    tenant_ids = {row["tenant_id"] for row in result["case_results"]}
    assert FIXTURE_TENANT_A in tenant_ids
    assert FIXTURE_TENANT_B in tenant_ids


def test_synthetic_matrix_covers_required_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SHADOW_MODE_ENV, "shadow")
    result = probe.execute_synthetic_matrix_probe(app_root=_REPO)
    case_ids = {row["case_id"] for row in result["case_results"]}
    assert case_ids == {
        "catalog_available_positive_claim",
        "catalog_unavailable_negative_claim",
        "irrelevant_turn_no_claim",
        "kb_catalog_conflict",
        "tenant_b_isolation",
        "unknown_entity_positive_claim",
        "variant_specific_conflict",
    }


def test_full_probe_summary_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_MODE_ENV, raising=False)
    summary = probe.execute_full_probe(app_root=_REPO, include_revision_gate=False)
    assert summary["ok"] is True
    assert summary["observation_window"]["duration_hours"] == "48"
    assert "teardown_command" in summary


def test_cli_default_off() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.operators.product_availability_truth_guard_shadow_observation", "default-off"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    assert payload["phase"] == PHASE_DEFAULT_OFF


def test_shadow_telemetry_schema() -> None:
    from modules.ai.brain.postprocess.product_availability_shadow_telemetry import (  # noqa: PLC0415
        SHADOW_TELEMETRY_SCHEMA_VERSION,
        aggregate_shadow_observations,
        build_shadow_observation,
        reset_turn_invocation_scope,
    )

    reset_turn_invocation_scope()
    row = build_shadow_observation(
        tenant_id=FIXTURE_TENANT_A,
        conversation_id=1,
        invocation_site="probe",
        guard_mode="shadow",
        evidence_state="conflict",
        conflict_type="KB_AVAILABLE_CATALOG_UNAVAILABLE",
        guard_action="rewrite_conflict",
        would_rewrite=True,
        reason="kb_catalog_mismatch",
        customer_text_changed=False,
        guard_duration_ms=3,
    ).to_dict()
    assert row["schema_version"] == SHADOW_TELEMETRY_SCHEMA_VERSION
    assert row["additional_llm_calls"] == 0
    assert row["customer_text_changed"] is False
    agg = aggregate_shadow_observations([row])
    assert agg["evaluated_turns"] == 1
    assert agg["would_rewrite_count"] == 1
