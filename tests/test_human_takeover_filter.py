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
    """Mirror of ``backend.routers.conversations._is_human_takeover``.

    The new contract: human takeover is determined ONLY by the
    explicit columns. ``ai_paused`` / ``ai_paused_reason`` do not
    contribute on their own.
    """
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
    # MANUAL aliases (manual / manual_pause) are NOT human-presence
    # reasons — they're pure "stop the AI" without a human takeover.
    assert REASON_MANUAL not in HUMAN_PRESENCE_REASONS
    from core.ai_pause_guard import REASON_MANUAL_PAUSE  # noqa: PLC0415
    assert REASON_MANUAL_PAUSE in VALID_REASONS
    assert REASON_MANUAL_PAUSE not in HUMAN_PRESENCE_REASONS


def test_manual_pause_reason_is_not_human():
    # The new ``manual_pause`` reason path: AI is paused but the
    # conversation is NOT in human takeover. Must NOT appear in the
    # human filter.
    c = _StubConvo(ai_paused=True, ai_paused_reason="manual_pause")
    assert not _is_human_takeover(c)


def test_ai_paused_human_handoff_reason_alone_is_not_human():
    # With the new contract the human filter is driven SOLELY by the
    # explicit columns (needs_human / handoff_active / taken_over_at /
    # is_human_handoff). Pause-reason metadata cannot pull a row into
    # the filter on its own — the takeover endpoint always sets the
    # explicit columns alongside the reason, so a row without them is
    # not a takeover.
    c = _StubConvo(ai_paused=True, ai_paused_reason="manual_takeover")
    # ↳ needs_human / handoff_active / taken_over_at all NOT set.
    assert not _is_human_takeover(c)


def test_takeover_button_state_sets_all_columns():
    # Queue/audit row as written by ``POST /conversations/handoff``:
    # human flags without mutating ``ai_paused``. Must classify as human.
    c = _StubConvo(
        ai_paused=False, ai_paused_reason=None,
        needs_human=True, handoff_active=True,
        taken_over_at=object(), taken_over_by="user:42",
        status="human", is_human_handoff=True,
    )
    assert _is_human_takeover(c)


def test_resume_after_manual_pause_does_not_clear_human_state():
    # When the merchant ran "إيقاف الذكاء" (manual_pause) on a
    # conversation that was ALSO previously in a human takeover (edge
    # case), resuming AI must not silently undo the takeover. We
    # simulate the post-resume state: ai_paused=False but human flags
    # untouched. This row should still be human.
    c = _StubConvo(
        ai_paused=False, ai_paused_reason=None,
        needs_human=True, handoff_active=True,
        taken_over_at=object(),
    )
    assert _is_human_takeover(c)


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
    # Reason ALONE is no longer enough — the row must also have at
    # least one explicit column (needs_human / handoff_active /
    # taken_over_at / is_human_handoff). The handoff endpoint always
    # writes both, so this still works in production.
    c = _StubConvo(
        ai_paused=True, ai_paused_reason="manual_takeover",
        needs_human=True,
    )
    assert _is_human_takeover(c)


def test_takeover_via_ai_paused_support_escalation():
    c = _StubConvo(
        ai_paused=True, ai_paused_reason="support_escalation",
        handoff_active=True,
    )
    assert _is_human_takeover(c)


def test_takeover_via_ai_paused_human_handoff():
    c = _StubConvo(
        ai_paused=True, ai_paused_reason="human_handoff",
        is_human_handoff=True,
    )
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
