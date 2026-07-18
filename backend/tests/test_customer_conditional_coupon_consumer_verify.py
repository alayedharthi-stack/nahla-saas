"""Contract and fail-closed tests for conditional-coupon consumer verify operator."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import (  # noqa: E402
    customer_conditional_coupon_consumer_verify as consumer_verify,
)
from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (  # noqa: E402
    CODE_COMMAND_INVALID,
    CODE_DB_GATE_SKIPPED,
    CODE_PINNED_REVISION_MISMATCH,
    CODE_TARGET_APP_ROOT_REQUIRED,
    COMPOSE_FLAG_ENV,
    PINNED_SOURCE_REVISION,
    PINNED_SOURCE_REVISION_SHORT,
    PINNED_TARGET_RUNTIME_REVISION,
    PROBE_DEDUP_SNAPSHOT_ID,
    REPORT_SCHEMA_VERSION,
    SHADOW_FLAG_ENV,
    normalize_pinned_revision,
    validate_gate_report,
    validate_summary_report,
)
from scripts.operators.customer_conditional_coupon_shadow_observation import (  # noqa: E402
    with_app_container_paths,
)


def test_normalize_pinned_revision_accepts_full_and_short() -> None:
    assert normalize_pinned_revision(PINNED_SOURCE_REVISION) == PINNED_SOURCE_REVISION
    assert normalize_pinned_revision(PINNED_SOURCE_REVISION_SHORT) == PINNED_SOURCE_REVISION


def test_normalize_pinned_revision_rejects_unknown() -> None:
    with pytest.raises(ValueError, match=CODE_PINNED_REVISION_MISMATCH):
        normalize_pinned_revision("deadbeef")


def test_projection_ineligible_shortfall_facts() -> None:
    with with_app_container_paths(_REPO):
        report = consumer_verify.gate_projection_ineligible()
    validate_gate_report(report)
    assert report["ok"] is True
    assert report["allow_min_orders_condition_claim"] is False
    assert report["evaluation"] == "condition_shortfall"
    assert report["facts_snapshot_id"]


def test_webhook_dedup_preserves_facts_snapshot_id() -> None:
    report = consumer_verify.gate_webhook_dedup_snapshot_persistence()
    validate_gate_report(report)
    assert report["ok"] is True
    assert report["saved_metadata"]["facts_snapshot_id"] == PROBE_DEDUP_SNAPSHOT_ID


def test_execute_consumer_verify_without_db_fails_db_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)
    monkeypatch.delenv(COMPOSE_FLAG_ENV, raising=False)
    summary = consumer_verify.execute_consumer_verify(
        app_root=_REPO,
        db=None,
        require_db_gates=True,
        require_runtime_attestation=False,
    )
    validate_summary_report(summary)
    assert summary["ok"] is False
    assert summary["results"]["a1_capability"] is False
    assert summary["results"]["teardown_flags"] is True


def test_teardown_clears_flags_after_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SHADOW_FLAG_ENV, "true")
    monkeypatch.setenv(COMPOSE_FLAG_ENV, "true")

    with patch.object(
        consumer_verify,
        "gate_projection_ineligible",
        side_effect=RuntimeError("probe_abort"),
    ):
        with pytest.raises(RuntimeError, match="probe_abort"):
            consumer_verify.execute_consumer_verify(
                app_root=_REPO,
                db=None,
                require_db_gates=False,
                require_runtime_attestation=False,
            )

    assert consumer_verify.gate_teardown_flags()["ok"] is True


def test_compose_persona_gate_fails_when_outbound_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(COMPOSE_FLAG_ENV, raising=False)
    fake_result = {
        "chosen_path": "customer_conditional_coupon_compose",
        "customer_conditional_coupon_compose_active": True,
        "facts_snapshot_id": "snap-test",
        "compose_source": "persona_llm",
    }
    with patch.object(
        consumer_verify,
        "_run_brain_compose_probe",
        return_value=(fake_result, ["materialise_for_customer"]),
    ):
        report = consumer_verify.gate_compose_persona_success()
    assert report["ok"] is False
    assert report["outbound_calls"] == ["materialise_for_customer"]


def test_compose_persona_gate_metadata_only_without_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(COMPOSE_FLAG_ENV, raising=False)
    fake_result = {
        "chosen_path": "customer_conditional_coupon_compose",
        "customer_conditional_coupon_compose_active": True,
        "facts_snapshot_id": "snap-test",
        "compose_source": "persona_llm",
        "response_mode": "customer_conditional_coupon",
        "final_customer_text_source": "persona_llm",
        "final_text_transformed": False,
        "llm_candidate_present": True,
        "conditional_coupon_telemetry": {"order_count_query_count": 1},
    }
    with patch.object(
        consumer_verify,
        "_run_brain_compose_probe",
        return_value=(fake_result, []),
    ):
        report = consumer_verify.gate_compose_persona_success()
    assert report["ok"] is True
    assert report["metadata"]["chosen_path"] == "customer_conditional_coupon_compose"
    assert report["outbound_calls"] == []


def test_shadow_observation_budget_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)
    with patch.object(
        consumer_verify.shadow_probe,
        "execute_shadow_observation_probe",
        return_value={
            "ok": True,
            "facts_count": 1,
            "subject_bridge_outcome": "resolved",
            "guards": {"materialise_for_customer_called": False},
            "telemetry": {
                "order_count_query_count": 2,
                "usage_evidence_query_count": 0,
            },
        },
    ):
        report = consumer_verify.gate_shadow_observation(MagicMock(), _REPO)
    assert report["ok"] is False
    assert report["query_budgets_ok"] is False


def test_cli_invalid_command_fail_closed() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.operators.customer_conditional_coupon_consumer_verify",
            "not-a-command",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["code"] == CODE_COMMAND_INVALID


def test_cli_default_off_subset_emits_closed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)
    monkeypatch.delenv(COMPOSE_FLAG_ENV, raising=False)
    report = consumer_verify.gate_default_off(_REPO)
    line = json.dumps(report, sort_keys=True, separators=(",", ":"))
    assert line.startswith("{")
    assert f'"report_schema_version":"{REPORT_SCHEMA_VERSION}"' in line.replace(" ", "")


def test_run_brain_compose_probe_raises_when_fixture_missing() -> None:
    with with_app_container_paths(_REPO):
        with patch.object(consumer_verify, "_get_fixture_conversation", return_value=None):
            with pytest.raises(RuntimeError, match=CODE_DB_GATE_SKIPPED):
                consumer_verify._run_brain_compose_probe(
                    persona_stub="stub",
                    llm_stub=None,
                )


def test_a1_capability_gate_shape() -> None:
    db = MagicMock()
    rev_result = MagicMock()
    rev_result.fetchall.return_value = [("0088",), ("0089",)]
    cap_result = MagicMock()
    cap_result.fetchone.return_value = ("validated", "0088")
    db.execute.side_effect = [rev_result, cap_result]
    report = consumer_verify.gate_a1_capability(db)
    validate_gate_report(report)
    assert report["ok"] is True
    assert report["capability_state"] == "validated"


def test_unsafe_guard_metadata_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(COMPOSE_FLAG_ENV, raising=False)
    fake_result = {
        "final_customer_text_source": "guard_rewrite",
        "final_text_transformed": True,
        "final_transform_reasons": ["customer_conditional_coupon_general_llm_evidence_guard"],
        "conditional_coupon_guard_failed_reason": "coupon_code_disclosure",
        "chosen_path": "customer_conditional_coupon_general_llm_fallthrough",
        "facts_snapshot_id": "snap-guard",
    }
    with patch.object(
        consumer_verify,
        "_run_brain_compose_probe",
        return_value=(fake_result, []),
    ):
        report = consumer_verify.gate_compose_general_llm_unsafe_guard()
    assert report["ok"] is True
    assert report["metadata"]["conditional_coupon_guard_failed_reason"] == "coupon_code_disclosure"


def test_artifact_preflight_passes_on_repo_root() -> None:
    report = consumer_verify.gate_artifact_preflight(_REPO)
    validate_gate_report(report)
    assert report["ok"] is True
    assert report["pinned_source_revision"] == PINNED_TARGET_RUNTIME_REVISION


def test_execute_consumer_verify_fails_without_runtime_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("NAHLA_CONSUMER_VERIFY_TARGET_APP_ROOT", raising=False)
    summary = consumer_verify.execute_consumer_verify(
        db=None,
        require_db_gates=False,
        require_runtime_attestation=True,
    )
    validate_summary_report(summary)
    assert summary["ok"] is False
    assert summary["code"] == CODE_TARGET_APP_ROOT_REQUIRED
    assert summary["results"]["runtime_revision_attestation"] is False


def test_operator_module_present_in_current_verifier_artifact() -> None:
    operator_path = (
        _REPO / "scripts" / "operators" / "customer_conditional_coupon_consumer_verify.py"
    )
    assert operator_path.is_file()


def test_artifact_preflight_fails_on_wrong_pin() -> None:
    report = consumer_verify.gate_artifact_preflight(
        _REPO,
        pinned_source_revision="00000000",
    )
    assert report["ok"] is False
    assert report["code"] == CODE_PINNED_REVISION_MISMATCH
