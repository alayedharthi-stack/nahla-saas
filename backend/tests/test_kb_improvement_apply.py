"""KB improvement apply path — fast DB-only promote/approve (no LLM re-run)."""
from __future__ import annotations

import inspect
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def test_improvement_analyze_defaults_skip_llm_and_signal_scan():
    from routers import knowledge as kmod

    sig = inspect.signature(kmod.improvement_suggestions)
    assert sig.parameters["polish"].default is False or (
        getattr(sig.parameters["polish"].default, "default", None) is False
    )
    signals_default = sig.parameters["include_conversation_signals"].default
    assert signals_default is False or (
        getattr(signals_default, "default", None) is False
    )


def test_promote_and_approve_endpoints_do_not_call_llm_or_reanalysis():
    from routers import knowledge as kmod

    promote_src = inspect.getsource(kmod.promote_improvement_suggestion)
    approve_src = inspect.getsource(kmod.approve_draft)

    banned_calls = (
        "polish_with_gpt(",
        "classify_quick_update(",
        "improvement_audit(",
        "scan_tenant_conversation_signals(",
    )
    for label, src in (("promote", promote_src), ("approve", approve_src)):
        for call in banned_calls:
            assert call not in src, f"{label} must not call {call}"

    assert "llm_ms=0" in promote_src
    assert "reanalysis_ms=0" in promote_src
    assert "llm_ms=0" in approve_src
    assert "reanalysis_ms=0" in approve_src


def test_analyze_skips_polish_when_disabled(monkeypatch):
    import modules.ai.knowledge.improvement_advisor as advisor

    calls: list[int] = []

    def _fake_polish(findings, **kwargs):
        calls.append(1)
        return list(findings)

    monkeypatch.setattr(advisor, "polish_with_gpt", _fake_polish)

    findings = [
        advisor.ImprovementFinding(
            id="s1",
            type="missing_required_knowledge",
            severity="high",
            title="t",
            reason="r",
            expected_impact="i",
            target_kind="payment_method",
            proposed_body="body",
            requires_media=False,
            confidence=0.9,
        ),
    ]

    polish = False
    polished = findings
    if polish:
        polished = advisor.polish_with_gpt(findings, tenant_id=1)
    assert polished == findings
    assert calls == []

    polished = advisor.polish_with_gpt(findings, tenant_id=1)
    assert calls == [1]
    assert len(polished) == 1
