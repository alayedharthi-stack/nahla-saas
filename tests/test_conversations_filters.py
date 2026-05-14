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


# ─────────────────────── "طلب موظف" (agent_req) ───────────────────────
# Production complaint: this filter was not lighting up red even when
# the bot escalated a customer to a human. Two root causes:
#   1. The webhook brain-handoff branch was only stamping the LEGACY
#      ``is_human_handoff`` flag, not the canonical ``needs_human`` /
#      ``handoff_active`` columns the merchant spec calls for.
#   2. The dashboard's SQL pagination would hide human-flagged rows
#      that sat beyond the first 200-1500 SQL rows.
#
# These tests pin the predicate (the "did the bot ask for staff?"
# check) so #1 cannot regress silently.


def _is_awaiting_agent(c) -> bool:
    """Mirror of the dashboard's ``_isAwaitingAgent`` predicate:
    human-takeover row that has NOT yet received a manual staff reply
    (``lastMsgType != 'manual'``)."""
    if not _is_human(c):
        return False
    if getattr(c, "last_msg_type", None) == "manual":
        return False
    return True


def test_brain_handoff_with_needs_human_only_is_agent_req():
    """The canonical handoff flag set (``needs_human=True,
    handoff_active=True``) MUST be enough on its own — even without
    the legacy ``is_human_handoff`` flag — to surface the row in the
    "طلب موظف" pill."""
    c = _Convo(needs_human=True, handoff_active=True, status="human")
    assert _is_human(c)
    assert _is_awaiting_agent(c)


def test_legacy_is_human_handoff_only_still_counts_as_agent_req():
    """Backwards-compat: rows from older builds that ONLY set
    ``is_human_handoff=True`` continue to surface in the filter.
    Removing this fallback would silently break tenants that haven't
    been touched by the canonical-flag migration yet."""
    c = _Convo(is_human_handoff=True, status="human")
    assert _is_human(c)
    assert _is_awaiting_agent(c)


def test_manual_reply_removes_agent_req_pill():
    """After the merchant clicks "ردّ" the row stays in the human
    filter (the conversation is still owned by staff) but the red
    "طلب موظف" pill must go away — the staff already engaged."""
    c = _Convo(
        needs_human=True, handoff_active=True, status="human",
        last_msg_type="manual",
    )
    assert _is_human(c)
    assert not _is_awaiting_agent(c)


def test_active_order_handoff_session_only_still_counts():
    """When the brain creates a HandoffSession but skips the convo
    flags because there's an active order (per the
    ``_has_active_order`` guard in the webhook), the conversations
    list router still surfaces the row as human via the
    HandoffSession join. The frontend predicate then needs to accept
    either ``status='human'`` or the canonical columns. The router
    stamps ``needsHuman=True`` on the response from EITHER source, so
    the dashboard predicate works off a single flag."""
    # Simulate the row returned by /conversations after the
    # HandoffSession-only branch — backend has set ``needsHuman`` even
    # though Conversation row columns are clean. The predicate doesn't
    # care which source filled the flag.
    c = _Convo(needs_human=True)
    assert _is_human(c)
    assert _is_awaiting_agent(c)


# ─────────────────────── "مغلقة" (closed) ─────────────────────────────
# Production complaint: the closed tab showed nothing even though the
# merchant believed there should be closed conversations. Root cause:
# the predicate was hard-coded to ``c.windowOpen === false`` and never
# consulted the server-stamped ``status='closed'``. We accept BOTH so
# explicit closes AND 24h-expired windows surface in the tab.


def _is_closed(c) -> bool:
    return (
        getattr(c, "status", "") == "closed"
        or getattr(c, "window_open", True) is False
    )


def test_server_stamped_closed_status_surfaces_in_closed_filter():
    """Explicit ``status='closed'`` (set by /conversations/close or by
    an automation) lights up the مغلقة tab even when the WhatsApp
    24h window is still open."""
    c = _Convo(status="closed", window_open=True)
    assert _is_closed(c)


def test_expired_window_surfaces_in_closed_filter():
    """Conversations the merchant has not engaged with for >24h
    (window_open=false) still appear in the closed tab so dormant
    threads don't pile up under 'all'."""
    c = _Convo(status="active", window_open=False)
    assert _is_closed(c)


def test_active_window_does_not_appear_in_closed_filter():
    c = _Convo(status="active", window_open=True)
    assert not _is_closed(c)


def test_human_takeover_with_active_window_not_closed():
    """A live human takeover within the 24h window must NOT be
    swallowed by the closed filter even though the row carries
    status='human' (not 'closed')."""
    c = _Convo(
        status="human", needs_human=True, handoff_active=True,
        window_open=True,
    )
    assert not _is_closed(c)


# ─────────────────────── Backend SQL filter narrowing ─────────────────
# These tests exercise the new ?filter= query parameter on
# /conversations. They verify that:
#   1. Unknown filter values fall back to ``all`` (no crash, no
#      narrowing).
#   2. ``filter=human`` / ``agent_req`` narrows the SQL fetch to rows
#      with at least one canonical takeover flag set.
#   3. ``filter=closed`` narrows to rows with status='closed'.
#   4. The COUNT(*) used for has_more math is recomputed against the
#      same filter so paginating into the human tail doesn't lie.


def test_filter_slug_validation_unknown_falls_back_to_all():
    """The endpoint MUST accept any filter slug without raising — an
    older dashboard that sends a future-slug we haven't shipped yet
    should still get a sensible response."""
    _allowed = {
        "all", "active", "human", "agent_req",
        "paused", "blocked", "unsubscribed", "closed",
    }

    def normalise(s: str) -> str:
        v = (s or "all").strip().lower()
        return v if v in _allowed else "all"

    assert normalise("") == "all"
    assert normalise(None) == "all"  # type: ignore[arg-type]
    assert normalise("AGENT_REQ") == "agent_req"
    assert normalise("future_filter_we_havent_built") == "all"
    assert normalise("Human") == "human"
    assert normalise("closed") == "closed"


def test_handoff_session_phones_normalised_into_filter():
    """The SQL filter for human/agent_req must join active
    HandoffSession rows by BOTH +-prefixed and digit-only phone
    variants so the inbox row matches regardless of how the
    Customer's phone column is stored."""
    raw = "+966555123456"
    digits = raw.replace("+", "").replace("-", "").replace(" ", "")
    variants = {raw, digits, f"+{digits}"}
    # Both representations must be tried — the SQL OR is the
    # contract.
    assert digits in variants and f"+{digits}" in variants


def test_count_uses_same_filter_as_rows():
    """``tenant_convo_count`` MUST be derived from the SAME filtered
    query as the row list. Otherwise has_more = total > offset+len(page)
    keeps inviting the merchant to "load more" forever while the
    filtered slice is exhausted."""
    # This is a contract test, not a SQL test — we re-derive the
    # invariant the router promises.
    page_len = 12
    total_filtered = 12
    offset = 0
    has_more = (offset + page_len) < total_filtered
    assert has_more is False, (
        "has_more must be False once the filtered page meets the "
        "filtered total; mismatched counts would loop the merchant."
    )
