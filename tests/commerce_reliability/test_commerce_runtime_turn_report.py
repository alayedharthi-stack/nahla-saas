"""The turn report's account of a stop, and the per-step tool ceiling, offline.

September 2026, Tenant 1 pilot: a turn ended ``stop_reason=provider_invalid``
and nothing in the log line or the terminal said why. The loop had the reason
all along (``tool_requests_exceed_declared_maximum:4>3``); the report now
carries it, bounded, and the ceiling itself follows the turn's tool budget.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.commerce_runtime import agent_contracts as ac  # noqa: E402
from core.commerce_runtime import runtime_entry as entry  # noqa: E402


def _outcome(status: str, stop_reason: str | None, detail: dict) -> ac.LoopOutcome:
    return ac.LoopOutcome(status=status, stop_reason=stop_reason, turn_id=1, delivery_sequence_id=None,
                          reused_delivery=False, state_revision=None, steps_used=2, tool_calls_used=1,
                          events=(), detail=detail)


def _budget(max_tool_calls: int) -> ac.LoopBudget:
    return ac.LoopBudget(max_steps=4, max_tool_calls=max_tool_calls, tool_timeout_seconds=10.0,
                         provider_timeout_seconds=15.0, deadline_seconds=45.0)


# ── The stop detail ──────────────────────────────────────────────────────────


def test_a_stopped_outcome_s_detail_is_carried_by_name_and_value() -> None:
    outcome = _outcome(ac.LoopStatus.STOPPED.value, ac.StopReason.PROVIDER_INVALID.value,
                       {"provider_reason": "tool_requests_exceed_declared_maximum:4>3"})
    assert entry._stop_detail(outcome) == (
        ("provider_reason", "tool_requests_exceed_declared_maximum:4>3"),)


def test_the_detail_is_sorted_and_nested_values_are_rendered_compactly() -> None:
    outcome = _outcome(ac.LoopStatus.STOPPED.value, ac.StopReason.OWNERSHIP_LOST.value,
                       {"rejection": "fence_stale", "original_reason": "budget_exhausted",
                        "original_detail": {"requested": 4, "limit": "max_tool_calls"},
                        "evidence_refs": ["catalog:product:1"]})
    detail = entry._stop_detail(outcome)
    assert [key for key, _ in detail] == ["original_detail", "original_reason", "rejection"]
    assert dict(detail)["original_detail"] == '{"limit": "max_tool_calls", "requested": 4}'
    assert "evidence_refs" not in dict(detail)          # it has its own field


def test_a_long_value_is_cut_and_the_item_count_is_bounded() -> None:
    long = "x" * 1000
    detail = {f"k{i:02d}": long for i in range(20)}
    outcome = _outcome(ac.LoopStatus.STOPPED.value, ac.StopReason.PROVIDER_FAILURE.value, detail)
    rendered = entry._stop_detail(outcome)
    assert len(rendered) == entry.STOP_DETAIL_MAX_ITEMS
    assert all(len(value) == entry.STOP_DETAIL_MAX_CHARS for _, value in rendered)
    assert all(value.endswith("…") for _, value in rendered)


def test_an_outcome_that_did_not_stop_carries_no_stop_detail() -> None:
    outcome = _outcome(ac.LoopStatus.PENDING_DELIVERY.value, None,
                       {"evidence_refs": ["catalog:product:1"], "kind": "text"})
    assert entry._stop_detail(outcome) == ()


def test_the_log_fields_render_the_detail_and_never_the_reply() -> None:
    report = entry.TurnReport(
        reason=entry.HANDLED, tenant_id=1, conversation_id=9, turn_id=6,
        loop_status=ac.LoopStatus.STOPPED.value, stop_reason=ac.StopReason.BUDGET_EXHAUSTED.value,
        stop_detail=(("limit", "max_tool_calls"), ("remaining", "3"), ("requested", "4")),
        reply_text="نص لا يظهر في السجل")
    fields = report.as_log_fields()
    assert fields["stop_detail"] == "limit=max_tool_calls;remaining=3;requested=4"
    assert "reply_text" not in fields and fields["reply_chars"] == len("نص لا يظهر في السجل")


def test_a_report_without_a_stop_renders_an_empty_detail() -> None:
    report = entry.TurnReport(reason=entry.HANDLED, tenant_id=1, conversation_id=9, turn_id=6)
    assert report.stop_detail == () and report.as_log_fields()["stop_detail"] == ""


# ── The per-step tool ceiling ────────────────────────────────────────────────


@pytest.mark.parametrize("max_tool_calls, ceiling", [(1, 1), (3, 3), (6, 6), (8, 8), (50, 8)])
def test_one_step_may_carry_as_many_requests_as_the_turn_can_pay_for(max_tool_calls: int,
                                                                        ceiling: int) -> None:
    assert entry._tool_requests_per_step(_budget(max_tool_calls)) == ceiling
    assert ceiling <= ac.MAX_TOOL_REQUESTS_PER_STEP


def test_the_default_pilot_budget_admits_a_four_request_bundle() -> None:
    """The observed bundle (four product-detail requests) fits the pilot's
    default tool budget of six, so it is run rather than refused."""
    from core.commerce_runtime import pilot_guard

    assert entry._tool_requests_per_step(pilot_guard.pilot_budget()) >= 4


def test_without_a_budget_the_loop_s_own_default_budget_applies() -> None:
    """``AgentLoop(budget=None)`` runs on ``LoopBudget()``; the ceiling mirrors it."""
    assert entry._tool_requests_per_step(None) == ac.LoopBudget().max_tool_calls
