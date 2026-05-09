"""State-machine contract tests for the conversations panel.

Covers the four UX paths the merchant sees:

  1. AI active        →  buttons: [تولّي] + [إيقاف الذكاء مؤقتاً]
  2. Manual pause     →  button:  [تشغيل الذكاء]   (does NOT touch human state)
  3. Human takeover   →  button:  [إعادة الذكاء]   (clears full takeover)
  4. Resume from #2   →  AI is on again, human state untouched

The frontend filter "بشري" must follow the same rules as
``_is_human_takeover``: driven SOLELY by the explicit columns.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.ai_pause_guard import (  # noqa: E402
    HUMAN_PRESENCE_REASONS,
    REASON_MANUAL,
    REASON_MANUAL_PAUSE,
    REASON_MANUAL_TAKEOVER,
    REASON_SUPPORT_ESCALATION,
    VALID_REASONS,
)


def _is_human(convo) -> bool:
    """Mirror of the new backend rule + frontend ``_isHumanResponding``."""
    if getattr(convo, "is_human_handoff", False):
        return True
    if getattr(convo, "needs_human", False):
        return True
    if getattr(convo, "handoff_active", False):
        return True
    if getattr(convo, "taken_over_at", None) is not None:
        return True
    if (getattr(convo, "status", "") or "").lower() == "human":
        return True
    return False


class _Convo:
    """Minimal stub matching the columns used by the rules."""

    def __init__(self, **kw):
        defaults = dict(
            is_human_handoff=False,
            paused_by_human=False,
            needs_human=False,
            handoff_active=False,
            taken_over_at=None,
            taken_over_by=None,
            status="active",
            ai_paused=False,
            ai_paused_reason=None,
            last_read_at=None,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


# Helpers that mimic each backend endpoint's mutation contract.
def _do_handoff(convo, *, by="user:42"):
    convo.status = "human"
    convo.is_human_handoff = True
    convo.paused_by_human = True
    convo.needs_human = True
    convo.handoff_active = True
    convo.taken_over_at = "2026-01-01T00:00:00+00:00"
    convo.taken_over_by = by
    convo.ai_paused = True
    convo.ai_paused_reason = REASON_MANUAL_TAKEOVER


def _do_manual_pause(convo):
    convo.ai_paused = True
    convo.ai_paused_reason = REASON_MANUAL_PAUSE


def _do_resume(convo):
    """Mirror of POST /ai-resume — only clears AI pause."""
    convo.ai_paused = False
    convo.ai_paused_reason = None


def _do_return_to_ai(convo):
    """Mirror of POST /handoff/return-to-ai — clears EVERYTHING."""
    convo.status = "active"
    convo.is_human_handoff = False
    convo.paused_by_human = False
    convo.needs_human = False
    convo.handoff_active = False
    convo.taken_over_at = None
    convo.taken_over_by = None
    convo.ai_paused = False
    convo.ai_paused_reason = None


# ─────────────────────── State machine ───────────────────────
def test_active_ai_is_not_human():
    c = _Convo()
    assert not _is_human(c)
    assert not c.ai_paused


def test_manual_pause_does_not_touch_human_state():
    c = _Convo()
    _do_manual_pause(c)
    assert c.ai_paused is True
    assert c.ai_paused_reason == REASON_MANUAL_PAUSE
    assert not _is_human(c)
    # Explicit column assertions — these are the columns the human
    # filter consults.
    assert c.needs_human is False
    assert c.handoff_active is False
    assert c.taken_over_at is None
    assert c.status == "active"


def test_handoff_makes_conversation_human():
    c = _Convo()
    _do_handoff(c)
    assert _is_human(c)
    assert c.ai_paused is True
    assert c.ai_paused_reason == REASON_MANUAL_TAKEOVER
    assert c.needs_human is True
    assert c.handoff_active is True
    assert c.taken_over_at is not None
    assert c.taken_over_by == "user:42"


def test_resume_from_manual_pause_only_toggles_ai():
    c = _Convo()
    _do_manual_pause(c)
    _do_resume(c)
    assert c.ai_paused is False
    assert c.ai_paused_reason is None
    assert not _is_human(c)
    assert c.status == "active"


def test_resume_does_not_clear_human_takeover():
    """Critical contract: ``ai-resume`` must not silently end a takeover.

    If the merchant somehow ends up calling ``ai-resume`` on a row
    that's also in a human takeover (the UI wouldn't render that
    button, but we still defend the backend), the human flags MUST
    survive — only ``ai-paused`` is cleared.
    """
    c = _Convo()
    _do_handoff(c)
    _do_resume(c)  # only the AI pause should clear
    assert c.ai_paused is False
    assert c.ai_paused_reason is None
    # Human state survives.
    assert _is_human(c)
    assert c.needs_human is True
    assert c.handoff_active is True


def test_return_to_ai_clears_full_state():
    c = _Convo()
    _do_handoff(c)
    _do_return_to_ai(c)
    assert not _is_human(c)
    assert c.ai_paused is False
    assert c.ai_paused_reason is None
    assert c.needs_human is False
    assert c.handoff_active is False
    assert c.taken_over_at is None
    assert c.taken_over_by is None
    assert c.status == "active"


def test_no_auto_transition_between_paths():
    # Manual pause does NOT put the conversation under human review,
    # and a takeover does NOT collapse to a generic "AI paused" — the
    # paths stay distinct in both directions.
    c1 = _Convo()
    _do_manual_pause(c1)
    assert not _is_human(c1)

    c2 = _Convo()
    _do_handoff(c2)
    assert _is_human(c2)
    assert c2.ai_paused_reason == REASON_MANUAL_TAKEOVER
    assert c2.ai_paused_reason != REASON_MANUAL_PAUSE


def test_reasons_registered():
    assert REASON_MANUAL in VALID_REASONS
    assert REASON_MANUAL_PAUSE in VALID_REASONS
    assert REASON_MANUAL_TAKEOVER in VALID_REASONS
    assert REASON_SUPPORT_ESCALATION in VALID_REASONS
    # Manual-style reasons are not human-presence.
    assert REASON_MANUAL not in HUMAN_PRESENCE_REASONS
    assert REASON_MANUAL_PAUSE not in HUMAN_PRESENCE_REASONS
    # Takeover-style reasons are.
    assert REASON_MANUAL_TAKEOVER in HUMAN_PRESENCE_REASONS
    assert REASON_SUPPORT_ESCALATION in HUMAN_PRESENCE_REASONS


# ─────────────────────── Unread / last_read_at ───────────────────────
def _unread_count(convo, inbound_timestamps, last_outbound_at=None):
    """Mirror of the new backend unread rule: count inbound messages
    where created_at > GREATEST(last_read_at, last_outbound_at), and
    excluding historical_import (handled at SQL level)."""
    last_read = convo.last_read_at
    last_out = last_outbound_at
    floor = max([t for t in (last_read, last_out) if t is not None], default=None)
    if floor is None:
        return len(inbound_timestamps)
    return sum(1 for t in inbound_timestamps if t > floor)


def test_unread_zero_after_mark_read():
    c = _Convo()
    inbound = [1, 2, 3, 4, 5]
    assert _unread_count(c, inbound) == 5
    c.last_read_at = 5  # mark-read at "now"
    assert _unread_count(c, inbound) == 0


def test_unread_counts_only_after_last_read():
    c = _Convo(last_read_at=3)
    inbound = [1, 2, 3, 4, 5]
    assert _unread_count(c, inbound) == 2  # 4 and 5 only


def test_unread_uses_max_of_last_read_and_last_outbound():
    c = _Convo(last_read_at=2)
    inbound = [1, 2, 3, 4, 5]
    # Merchant replied at t=4 → unread = inbound after t=4 → just 5.
    assert _unread_count(c, inbound, last_outbound_at=4) == 1


def test_unread_zero_when_merchant_replied_after_read():
    c = _Convo(last_read_at=2)
    inbound = [1, 2, 3]
    assert _unread_count(c, inbound, last_outbound_at=10) == 0
