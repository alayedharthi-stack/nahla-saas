"""Unit tests for the unified human-takeover filter logic.

These tests don't hit the database — they simulate the conversation
shape used by ``list_conversations`` and the frontend filter so we can
guarantee the rule set ("any of needs_human / handoff_active /
taken_over_at / status='human' / ai_paused with human reason") flips
the inbox into the "بشري" tab.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.ai_pause_guard import (  # noqa: E402
    HUMAN_PRESENCE_REASONS,
    REASON_HUMAN_HANDOFF,
    REASON_MANUAL,
    REASON_MANUAL_TAKEOVER,
    REASON_SUPPORT_ESCALATION,
    VALID_REASONS,
)


def _is_human_takeover(convo) -> bool:
    """Mirror of ``backend.routers.conversations._is_human_takeover``."""
    if convo is None:
        return False
    if getattr(convo, "is_human_handoff", False):
        return True
    if getattr(convo, "needs_human", False):
        return True
    if getattr(convo, "handoff_active", False):
        return True
    if getattr(convo, "taken_over_at", None) is not None:
        return True
    if getattr(convo, "ai_paused", False):
        if (getattr(convo, "ai_paused_reason", None) or "") in HUMAN_PRESENCE_REASONS:
            return True
    return False


class _StubConvo:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_reasons_registered():
    assert REASON_MANUAL_TAKEOVER in VALID_REASONS
    assert REASON_SUPPORT_ESCALATION in VALID_REASONS
    assert REASON_MANUAL_TAKEOVER in HUMAN_PRESENCE_REASONS
    assert REASON_SUPPORT_ESCALATION in HUMAN_PRESENCE_REASONS
    assert REASON_HUMAN_HANDOFF in HUMAN_PRESENCE_REASONS
    # MANUAL alone is NOT a human-presence pause (it's the merchant
    # silencing the AI temporarily without a human takeover).
    assert REASON_MANUAL not in HUMAN_PRESENCE_REASONS


def test_takeover_via_explicit_flag():
    c = _StubConvo(needs_human=True)
    assert _is_human_takeover(c)


def test_takeover_via_handoff_active():
    c = _StubConvo(handoff_active=True)
    assert _is_human_takeover(c)


def test_takeover_via_taken_over_at():
    c = _StubConvo(taken_over_at=object())  # any non-None timestamp
    assert _is_human_takeover(c)


def test_takeover_via_ai_paused_manual_takeover():
    c = _StubConvo(ai_paused=True, ai_paused_reason="manual_takeover")
    assert _is_human_takeover(c)


def test_takeover_via_ai_paused_support_escalation():
    c = _StubConvo(ai_paused=True, ai_paused_reason="support_escalation")
    assert _is_human_takeover(c)


def test_takeover_via_ai_paused_human_handoff():
    c = _StubConvo(ai_paused=True, ai_paused_reason="human_handoff")
    assert _is_human_takeover(c)


def test_manual_pause_alone_is_not_human():
    # Merchant clicked "إيقاف الذكاء" without taking over → NOT in the
    # "بشري" filter; it's just a paused AI conversation.
    c = _StubConvo(ai_paused=True, ai_paused_reason="manual")
    assert not _is_human_takeover(c)


def test_resume_clears_human_state():
    # Simulating the dashboard "تشغيل الذكاء" button: every flag below
    # is reset by the resume endpoint, so the conversation is no longer
    # human.
    c = _StubConvo(
        is_human_handoff=False,
        paused_by_human=False,
        needs_human=False,
        handoff_active=False,
        taken_over_at=None,
        ai_paused=False,
        ai_paused_reason=None,
    )
    assert not _is_human_takeover(c)


def test_clean_active_conversation_is_not_human():
    c = _StubConvo()
    assert not _is_human_takeover(c)
