"""Tests for staff escalation truth guard — blocks false escalation claims."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.staff_escalation_evidence import (  # noqa: E402
    evaluate_staff_escalation_evidence,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
    reply_contains_escalation_claim,
)


class TestBlockedFalseEscalation:
    def test_blocks_transfer_claim_without_evidence(self) -> None:
        llm_reply = "تم تحويلك لفريق الدعم، راح يتواصلون معك قريباً 🌷"
        result = apply_staff_escalation_truth_guard(
            reply=llm_reply,
            conversation_flags={"needs_human": False, "handoff_active": False},
            tenant_id=33,
            conversation_id=9001,
        )
        assert result.replaced is True
        assert result.staff_escalation_claim_blocked is True
        assert result.reply == SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "سأخبر" not in result.reply
        assert "فريق" not in result.reply

    def test_blocks_team_notification_claim_without_evidence(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم إشعار الفريق وسيتابعون معك",
            conversation_flags={},
        )
        assert result.replaced is True
        assert result.reply == SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR

    def test_needs_human_alone_is_not_evidence(self) -> None:
        evidence = evaluate_staff_escalation_evidence(
            conversation_flags={
                "needs_human": True,
                "handoff_active": False,
                "is_human_handoff": False,
                "status": "active",
            },
        )
        assert evidence.evidence_ok is False

        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم",
            conversation_flags={
                "needs_human": True,
                "handoff_active": False,
                "is_human_handoff": False,
                "status": "active",
            },
        )
        assert result.replaced is True


class TestAllowedWithEvidence:
    def test_allows_when_brain_handoff_session_created(self) -> None:
        llm_reply = "تم تحويلك لفريق الدعم، شكراً لصبرك 🌷"
        result = apply_staff_escalation_truth_guard(
            reply=llm_reply,
            brain_handoff=True,
            conversation_flags={
                "needs_human": True,
                "handoff_active": True,
                "is_human_handoff": True,
                "status": "human",
            },
        )
        assert result.replaced is False
        assert result.action == "allowed"
        assert result.reply == llm_reply

    def test_allows_when_active_handoff_state(self) -> None:
        llm_reply = "تم تحويلك لفريق المتجر"
        result = apply_staff_escalation_truth_guard(
            reply=llm_reply,
            conversation_flags={
                "needs_human": True,
                "handoff_active": True,
                "is_human_handoff": True,
                "status": "human",
            },
        )
        assert result.replaced is False
        assert result.action == "allowed"

    def test_allows_deterministic_handoff_path(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم",
            chosen_path="ACTION_HANDOFF",
        )
        assert result.replaced is False
        assert result.action == "allowed"

    def test_allows_pre_brain_handoff_metadata(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويل المحادثة لفريق المتجر",
            inbound_metadata={
                "deterministic_path": "pre_brain_handoff:clear",
                "handoff_active": True,
                "event_type": "ai_handoff_ack",
            },
        )
        assert result.replaced is False


class TestEscalationClaimDetection:
    @pytest.mark.parametrize(
        "phrase",
        [
            "تم تحويلك للدعم",
            "تم إشعار الفريق",
            "تم رفع الطلب",
            "تم التصعيد",
            "سيتم تحويلك الآن",
            "أحولك للفريق الآن",
        ],
    )
    def test_markers_detected(self, phrase: str) -> None:
        assert reply_contains_escalation_claim(phrase) is True

    def test_safe_stub_has_no_operational_promise(self) -> None:
        assert reply_contains_escalation_claim(SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR) is False
        assert "سأخبر" not in SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "فريق" not in SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR

    def test_blocked_result_exposes_persona_hook_metadata(self) -> None:
        from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: PLC0415
            guard_metadata_patch,
        )

        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم",
            conversation_flags={},
        )
        patch = guard_metadata_patch(result)
        assert patch.get("staff_escalation_claim_blocked") is True
        assert patch.get("staff_escalation_guard_reason")

    def test_persona_small_talk_not_flagged(self) -> None:
        assert reply_contains_escalation_claim("أنا نحلة، كيف أقدر أخدمك؟") is False
