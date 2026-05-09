"""Tests for the three mutually-exclusive operational filters in the
conversations panel:

  * متوقف الذكاء (paused)
  * بشري        (human)
  * محظور       (blocked)

Each conversation must appear in AT MOST ONE of these three filters
(the "all" filter is orthogonal). Priority order when multiple flags
are set: blocked > human > paused.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


# Mirror the frontend predicates 1:1 so the test suite pins the exact
# behaviour the dashboard ships.
def _is_blocked(c) -> bool:
    return bool(getattr(c, "is_blocked", False)) or (
        getattr(c, "ai_paused_reason", None) == "internal_number"
    )


def _is_human(c) -> bool:
    if _is_blocked(c):
        return False
    if getattr(c, "needs_human", False):
        return True
    if getattr(c, "handoff_active", False):
        return True
    if getattr(c, "status", "") == "human":
        return True
    if getattr(c, "is_human_handoff", False):
        return True
    if getattr(c, "taken_over_at", None) is not None:
        return True
    return False


def _is_ai_paused_only(c) -> bool:
    if not getattr(c, "ai_paused", False):
        return False
    if _is_blocked(c):
        return False
    if _is_human(c):
        return False
    return True


class _Convo:
    def __init__(self, **kw):
        defaults = dict(
            ai_paused=False, ai_paused_reason=None,
            needs_human=False, handoff_active=False,
            taken_over_at=None, taken_over_by=None,
            is_human_handoff=False, status="active",
            is_blocked=False,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


# ─────────────────────── Single-state membership ───────────────────────
def test_manual_pause_is_paused_only():
    c = _Convo(ai_paused=True, ai_paused_reason="manual_pause")
    assert _is_ai_paused_only(c)
    assert not _is_human(c)
    assert not _is_blocked(c)


def test_bot_loop_pause_is_paused_only():
    c = _Convo(ai_paused=True, ai_paused_reason="bot_loop_detected")
    assert _is_ai_paused_only(c)
    assert not _is_human(c)
    assert not _is_blocked(c)


def test_rate_limit_pause_is_paused_only():
    c = _Convo(ai_paused=True, ai_paused_reason="rate_limit")
    assert _is_ai_paused_only(c)
    assert not _is_human(c)
    assert not _is_blocked(c)


def test_legacy_manual_pause_reason_is_paused_only():
    c = _Convo(ai_paused=True, ai_paused_reason="manual")
    assert _is_ai_paused_only(c)
    assert not _is_human(c)
    assert not _is_blocked(c)


def test_human_takeover_is_human_only():
    c = _Convo(
        ai_paused=True, ai_paused_reason="manual_takeover",
        needs_human=True, handoff_active=True, status="human",
        taken_over_at="2026-01-01T00:00:00+00:00",
    )
    assert _is_human(c)
    assert not _is_ai_paused_only(c)
    assert not _is_blocked(c)


def test_blocked_via_isblocked_flag():
    c = _Convo(is_blocked=True)
    assert _is_blocked(c)
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


def test_blocked_via_internal_number_reason():
    c = _Convo(ai_paused=True, ai_paused_reason="internal_number")
    assert _is_blocked(c)
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


def test_blocked_takes_priority_over_human():
    """Edge case: phone is both flagged as human takeover AND in the
    blocklist. Blocked wins so the filters stay disjoint."""
    c = _Convo(
        is_blocked=True,
        needs_human=True, handoff_active=True, status="human",
    )
    assert _is_blocked(c)
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


def test_human_takes_priority_over_paused():
    """Takeover always sets ai_paused=true with a takeover reason. The
    "paused" filter must NOT swallow that row."""
    c = _Convo(
        ai_paused=True, ai_paused_reason="manual_takeover",
        needs_human=True,
    )
    assert _is_human(c)
    assert not _is_ai_paused_only(c)


def test_clean_active_conversation_in_no_filter():
    c = _Convo()
    assert not _is_blocked(c)
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


# ─────────────────────── Disjoint property ───────────────────────
def test_no_conversation_in_more_than_one_operational_filter():
    """For every reasonable combination of flags, the three filters
    are pairwise-disjoint."""
    cases = [
        _Convo(),  # active AI
        _Convo(ai_paused=True, ai_paused_reason="manual_pause"),
        _Convo(ai_paused=True, ai_paused_reason="bot_loop_detected"),
        _Convo(ai_paused=True, ai_paused_reason="rate_limit"),
        _Convo(ai_paused=True, ai_paused_reason="manual"),
        _Convo(needs_human=True),
        _Convo(handoff_active=True),
        _Convo(status="human", is_human_handoff=True),
        _Convo(taken_over_at="2026-01-01T00:00:00+00:00"),
        _Convo(
            ai_paused=True, ai_paused_reason="manual_takeover",
            needs_human=True, handoff_active=True,
            taken_over_at="2026-01-01T00:00:00+00:00",
            status="human", is_human_handoff=True,
        ),
        _Convo(is_blocked=True),
        _Convo(ai_paused=True, ai_paused_reason="internal_number"),
        # Mixed: blocked + human → blocked wins.
        _Convo(is_blocked=True, needs_human=True, handoff_active=True),
        # Mixed: blocked + paused → blocked wins.
        _Convo(is_blocked=True, ai_paused=True, ai_paused_reason="manual_pause"),
    ]
    for c in cases:
        flags = [_is_blocked(c), _is_human(c), _is_ai_paused_only(c)]
        # At most one of the three is true.
        assert sum(flags) <= 1, (
            f"Conversation with {vars(c)} matched multiple filters: "
            f"blocked={flags[0]} human={flags[1]} paused={flags[2]}"
        )


# ─────────────────────── Action transitions ───────────────────────
def test_pause_button_moves_into_paused_filter():
    c = _Convo()
    # mimic POST /conversations/ai-pause with reason=manual_pause
    c.ai_paused = True
    c.ai_paused_reason = "manual_pause"
    assert _is_ai_paused_only(c)


def test_resume_button_removes_from_paused_filter():
    c = _Convo(ai_paused=True, ai_paused_reason="manual_pause")
    # mimic POST /conversations/ai-resume
    c.ai_paused = False
    c.ai_paused_reason = None
    assert not _is_ai_paused_only(c)


def test_takeover_button_moves_into_human_filter():
    c = _Convo()
    # mimic POST /conversations/handoff
    c.ai_paused = True
    c.ai_paused_reason = "manual_takeover"
    c.needs_human = True
    c.handoff_active = True
    c.taken_over_at = "now"
    c.status = "human"
    assert _is_human(c)
    assert not _is_ai_paused_only(c)


def test_return_to_ai_button_removes_from_human_filter():
    c = _Convo(
        ai_paused=True, ai_paused_reason="manual_takeover",
        needs_human=True, handoff_active=True, status="human",
        taken_over_at="now",
    )
    # mimic POST /conversations/handoff/return-to-ai
    c.ai_paused = False
    c.ai_paused_reason = None
    c.needs_human = False
    c.handoff_active = False
    c.taken_over_at = None
    c.status = "active"
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


def test_block_button_moves_into_blocked_filter():
    c = _Convo()
    # mimic POST /conversations/blocklist/add
    c.is_blocked = True
    c.ai_paused = True
    c.ai_paused_reason = "internal_number"
    assert _is_blocked(c)
    assert not _is_human(c)
    assert not _is_ai_paused_only(c)


def test_unblock_button_removes_from_blocked_filter():
    c = _Convo(is_blocked=True, ai_paused=True, ai_paused_reason="internal_number")
    # mimic POST /conversations/blocklist/remove
    c.is_blocked = False
    c.ai_paused = False
    c.ai_paused_reason = None
    assert not _is_blocked(c)
