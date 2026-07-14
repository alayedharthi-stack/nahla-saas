"""Unit tests for Trusted Context shadow production telemetry audit (synthetic logs only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.prod_telemetry_audit import (  # noqa: E402
    DEFAULT_MIN_SAMPLES_FOR_PASS,
    _BASE_ACCEPTANCE_GAPS,
    TelemetryVerdict,
    audit_shadow_telemetry,
    parse_shadow_log_lines,
)


def _success_line(**overrides) -> str:
    payload = {
        "event": "TRUSTED_CONTEXT_SHADOW",
        "snapshot_id": "abc123",
        "tenant_id": 1,
        "conversation_id": 42,
        "customer_phone_tail": "0099",
        "loaded_domains": ["customer"],
        "sources": ["order_context_builder"],
        "fact_count": 3,
        "shadow_observability": {"loader_duration_ms": 12, "coupon_count": 0},
    }
    payload.update(overrides)
    return f'INFO nahla.brain.trusted_context [TRUSTED_CONTEXT_SHADOW] {json.dumps(payload)}'


def _error_line(kind: str = "build_failed", **fields) -> str:
    defaults = {
        "tenant": 1,
        "stage": "build",
        "error_class": "RuntimeError",
    }
    defaults.update(fields)
    return (
        f'WARNING nahla.brain.trusted_context [TRUSTED_CONTEXT_SHADOW] {kind} '
        f'tenant={defaults["tenant"]} stage={defaults["stage"]} '
        f'error_class={defaults["error_class"]}'
    )


def test_parse_counts_only_shadow_marker_lines() -> None:
    lines = [
        "unrelated log line",
        _success_line(),
        _error_line(),
    ]
    samples = parse_shadow_log_lines(lines)
    assert len(samples) == 2
    assert sum(1 for s in samples if s.is_success) == 1
    assert sum(1 for s in samples if s.is_error_event) == 1


def test_pass_on_clean_success_sample_with_explicit_min_samples() -> None:
    report = audit_shadow_telemetry([_success_line()], min_samples_for_pass=1)
    assert report.telemetry_log_safety_verdict == TelemetryVerdict.PASS
    assert report.verdict == TelemetryVerdict.PASS
    assert report.success_event_count == 1
    assert report.forbidden_leak_count == 0
    assert report.has_loader_duration is True
    assert report.snapshot_event_duplicate_count == 0
    assert report.required_min_samples == 1
    assert report.acceptance_gaps == _BASE_ACCEPTANCE_GAPS


def test_audit_rejects_invalid_min_samples() -> None:
    with pytest.raises(ValueError, match="min_samples_for_pass must be an integer >= 1"):
        audit_shadow_telemetry([_success_line()], min_samples_for_pass=0)
    with pytest.raises(ValueError, match="min_samples_for_pass must be an integer >= 1"):
        audit_shadow_telemetry([_success_line()], min_samples_for_pass=-3)


def test_cli_rejects_invalid_min_samples() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "backend/scripts/_trusted_context_prod_telemetry_once.py",
            "--min-samples",
            "0",
        ],
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode != 0
    assert "min-samples must be an integer >= 1" in proc.stderr


def test_default_min_samples_requires_twenty_for_pass() -> None:
    lines = [_success_line(snapshot_id=f"snap-{i}") for i in range(19)]
    report = audit_shadow_telemetry(lines)
    assert report.required_min_samples == DEFAULT_MIN_SAMPLES_FOR_PASS
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP
    assert "insufficient_success_event_count" in report.notes

    lines.append(_success_line(snapshot_id="snap-19"))
    report = audit_shadow_telemetry(lines)
    assert report.success_event_count == 20
    assert report.verdict == TelemetryVerdict.PASS


def test_pass_with_followup_when_no_success_events() -> None:
    report = audit_shadow_telemetry([_error_line()], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP
    assert report.success_event_count == 0
    assert report.error_event_count == 1
    assert report.unsafe_error_event_count == 0
    assert "no_trusted_context_shadow_success_events" in report.notes


def test_pass_with_followup_on_duplicate_snapshot_events() -> None:
    line = _success_line()
    report = audit_shadow_telemetry([line, line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP
    assert report.snapshot_event_duplicate_count == 1
    assert "duplicate_snapshot_log_events_detected" in report.notes


def test_pass_with_followup_when_loader_duration_missing() -> None:
    payload = {
        "event": "TRUSTED_CONTEXT_SHADOW",
        "snapshot_id": "abc123",
        "tenant_id": 1,
        "conversation_id": 42,
        "customer_phone_tail": "0099",
        "fact_count": 1,
    }
    line = f'[TRUSTED_CONTEXT_SHADOW] {json.dumps(payload)}'
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP
    assert report.has_loader_duration is False
    assert "loader_duration_ms_missing" in report.notes


def test_fail_on_facts_leak() -> None:
    line = _success_line()
    line = line.replace('"fact_count": 3', '"facts": {"order": {"status": "draft"}}, "fact_count": 3')
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.forbidden_leak_count > 0


def test_fail_on_full_coupon_code() -> None:
    line = _success_line(code="SECRET10")
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL


def test_fail_on_raw_promotion_conditions() -> None:
    obs = {"loader_duration_ms": 9, "applicable_products": ["sku-1"]}
    line = _success_line(shadow_observability=obs)
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL


def test_fail_on_full_phone() -> None:
    line = _success_line(customer_phone="966500000099")
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL


def test_fail_on_exception_message_in_line() -> None:
    line = _success_line() + " Exception: database connection refused"
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL


@pytest.mark.parametrize(
    ("suffix",),
    [
        (" code=secret10",),
        (" CODE=SECRET10",),
        (" applicable_products=[sku-1]",),
        (" conditions=min_cart_total:100",),
        (" promotion_conditions=stackable",),
        (" raw_conditions=sku_list",),
    ],
)
def test_fail_on_raw_kv_leak_patterns_outside_json(suffix: str) -> None:
    line = _success_line() + suffix
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.forbidden_leak_count > 0


def test_safe_error_event_counts_without_fail() -> None:
    report = audit_shadow_telemetry(
        [_error_line(), _error_line(kind="wire_failed", stage="wire", error_class="TimeoutError")],
        min_samples_for_pass=1,
    )
    assert report.error_event_count == 2
    assert report.unsafe_error_event_count == 0
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP


def test_safe_layer2_failed_error_event_counts_without_fail() -> None:
    line = (
        "WARNING nahla.brain.trusted_context [TRUSTED_CONTEXT_SHADOW] layer2_failed "
        "tenant=1 stage=layer2_compare error_class=RuntimeError"
    )
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.error_event_count == 1
    assert report.unsafe_error_event_count == 0
    assert report.verdict == TelemetryVerdict.PASS_WITH_FOLLOW_UP


def test_pass_with_safe_nested_layer2_shadow_metadata() -> None:
    from modules.ai.brain.truth_surface.contract import TrustedContextSnapshot  # noqa: E402
    from modules.ai.brain.truth_surface.layer2 import (  # noqa: E402
        build_decision_plan_shadow,
        build_intent_evidence,
    )

    snapshot = TrustedContextSnapshot(
        tenant_id=1,
        customer_phone="966500000099",
        loaded_domains=["customer", "capabilities", "promotions"],
        facts=[],
    )
    snapshot.ensure_snapshot_id()
    evidence = build_intent_evidence(
        message="offer please",
        source_turn_ref="snap-test-ref",
    )
    plan = build_decision_plan_shadow(evidence=evidence, snapshot=snapshot)
    obs = {
        "loader_duration_ms": 12,
        "coupon_count": 0,
        "layer2_shadow": {
            "status": "ok",
            "intent_evidence": evidence.to_dict(),
            "decision_plan": plan.to_metadata(),
            "duration_ms": 4,
        },
    }
    line = _success_line(shadow_observability=obs)
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.PASS
    assert report.forbidden_leak_count == 0


def test_fail_on_nested_layer2_shadow_sensitive_leak() -> None:
    obs = {
        "loader_duration_ms": 12,
        "layer2_shadow": {
            "status": "ok",
            "intent_evidence": {"facts": {"code": "SECRET10"}},
            "decision_plan": {"proposed_action": "no_op_shadow"},
            "duration_ms": 2,
        },
    }
    line = _success_line(shadow_observability=obs)
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.forbidden_leak_count > 0


def test_fail_on_unsafe_error_event_with_arbitrary_text() -> None:
    line = _error_line() + " err=database timeout secret"
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.error_event_count == 1
    assert report.unsafe_error_event_count == 1
    assert "unsafe_error_events_detected" in report.notes


def test_fail_on_unsafe_error_event_with_coupon_code() -> None:
    line = (
        "[TRUSTED_CONTEXT_SHADOW] build_failed tenant=1 stage=build "
        "error_class=RuntimeError code=SECRET10"
    )
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.unsafe_error_event_count == 1


def test_fail_on_unsafe_error_event_with_traceback() -> None:
    line = _error_line() + "\nTraceback (most recent call last):"
    report = audit_shadow_telemetry([line], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.unsafe_error_event_count == 1


def test_script_reads_stdin_with_explicit_min_samples() -> None:
    line = _success_line()
    proc = subprocess.run(
        [
            sys.executable,
            "backend/scripts/_trusted_context_prod_telemetry_once.py",
            "--min-samples",
            "1",
        ],
        input=line + "\n",
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["audit"]["telemetry_log_safety_verdict"] == "PASS"
    assert out["audit"]["verdict"] == "PASS"
    assert out["acceptance_gaps"] == list(_BASE_ACCEPTANCE_GAPS)
    assert out["required_min_samples"] == 1
    assert out["source"] == "stdin"


def test_script_reads_local_file_with_default_min_samples(tmp_path) -> None:
    log_file = tmp_path / "shadow.log"
    log_file.write_text(_success_line() + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "backend/scripts/_trusted_context_prod_telemetry_once.py",
            "--file",
            str(log_file),
        ],
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["required_min_samples"] == DEFAULT_MIN_SAMPLES_FOR_PASS
    assert out["audit"]["telemetry_log_safety_verdict"] == "PASS_WITH_FOLLOW_UP"
    assert out["acceptance_gaps"] == list(_BASE_ACCEPTANCE_GAPS)
    assert out["source"] == str(log_file)


def test_script_stdin_pass_with_followup_exits_zero_without_require_pass() -> None:
    line = _success_line()
    proc = subprocess.run(
        [sys.executable, "backend/scripts/_trusted_context_prod_telemetry_once.py"],
        input=line + "\n",
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["audit"]["telemetry_log_safety_verdict"] == "PASS_WITH_FOLLOW_UP"


def test_script_stdin_pass_with_followup_exits_nonzero_with_require_pass() -> None:
    line = _success_line()
    proc = subprocess.run(
        [
            sys.executable,
            "backend/scripts/_trusted_context_prod_telemetry_once.py",
            "--require-pass",
        ],
        input=line + "\n",
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode == 1, proc.stderr
    out = json.loads(proc.stdout)
    assert out["audit"]["telemetry_log_safety_verdict"] == "PASS_WITH_FOLLOW_UP"


def test_script_file_pass_with_followup_exits_nonzero_with_require_pass(tmp_path) -> None:
    log_file = tmp_path / "shadow.log"
    log_file.write_text(_success_line() + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "backend/scripts/_trusted_context_prod_telemetry_once.py",
            "--file",
            str(log_file),
            "--require-pass",
        ],
        capture_output=True,
        text=True,
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        timeout=30,
    )
    assert proc.returncode == 1, proc.stderr
    out = json.loads(proc.stdout)
    assert out["audit"]["telemetry_log_safety_verdict"] == "PASS_WITH_FOLLOW_UP"
    assert out["source"] == str(log_file)


def test_audit_rejects_nested_trusted_coupon_offer_compose_leaks() -> None:
    leaky = _success_line(
        trusted_coupon_offer_compose={
            "status": "ok",
            "surface": "trusted_coupon_offer_answer",
            "code": "SAVE10",
        }
    )
    report = audit_shadow_telemetry([leaky], min_samples_for_pass=1)
    assert report.verdict == TelemetryVerdict.FAIL
    assert report.forbidden_leak_count >= 1


def test_audit_accepts_safe_trusted_coupon_offer_compose_metadata() -> None:
    safe = _success_line(
        trusted_coupon_offer_compose={
            "status": "ok",
            "surface": "trusted_coupon_offer_answer",
            "question_kind": "offer",
            "facts_snapshot_id": "abc123def456",
        }
    )
    report = audit_shadow_telemetry([safe], min_samples_for_pass=1)
    assert report.forbidden_leak_count == 0
    assert report.verdict in {TelemetryVerdict.PASS, TelemetryVerdict.PASS_WITH_FOLLOW_UP}

