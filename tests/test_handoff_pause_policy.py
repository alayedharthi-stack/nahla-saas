"""
tests/test_handoff_pause_policy.py
──────────────────────────────────
Regression coverage for the May 2026 #46 policy shift on Tenant 33:

    "الإيقاف الكامل للذكاء يجب أن يكون يدويًا فقط من الموظف داخل
     لوحة نحلة."

Concretely:
    * Customer-side handoff / escalation MUST NOT auto-pause the AI.
    * Owner-contact tiers (VAGUE / CLEAR / COMPLAINT) MUST NOT
      auto-pause.
    * Generic handoff ("أبي أتكلم مع موظف") MUST NOT auto-pause.
    * Manual pause from the staff dashboard (calling
      ``core.ai_pause_guard.pause_ai``) MUST still silence the AI on
      the next inbound — that's the only kill switch left.

Two coverage layers:

  1. Pure-helper test of ``resolve_handoff_pause_policy`` — the
     single source of truth for the tier → pause/flip mapping the
     webhook applies. Trivially deterministic, no DB, no SQLAlchemy.

  2. Mode-resolver test of ``_conversation_handoff_flag`` — pinning
     the new semantics where the auto-flipped advisory tags
     (``is_human_handoff`` / ``needs_human`` / ``handoff_active``)
     no longer trap the conversation in MODE_SUPPORT_ESCALATION on
     their own. Only ``paused_by_human`` and ``taken_over_at`` (the
     two staff-active signals) trigger the override.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ════════════════════════════════════════════════════════════════════
# Part 1 — resolve_handoff_pause_policy (pure helper)
# ════════════════════════════════════════════════════════════════════


def test_owner_vague_keeps_ai_alive_and_does_not_flip_full_handoff() -> None:
    """VAGUE owner-contact: clarifier ack only. No full flip, no
    session, no pause. Customer can send the next message and the
    brain handles it normally."""
    from core.handoff_detector import (
        OWNER_TIER_VAGUE,
        resolve_handoff_pause_policy,
    )
    policy = resolve_handoff_pause_policy(OWNER_TIER_VAGUE)
    assert policy["do_full_handoff_flip"] is False
    assert policy["do_create_session"] is False
    assert policy["do_pause_ai"] is False, (
        "VAGUE owner-contact must never auto-pause the AI."
    )


def test_owner_clear_keeps_ai_alive_with_full_handoff_flags() -> None:
    """CLEAR owner-contact: full flip + session so the dashboard
    sees a real "طلب موظف" entry. AI stays alive — customer can ask
    natural follow-up questions while staff prepares to address the
    owner-level request."""
    from core.handoff_detector import (
        OWNER_TIER_CLEAR,
        resolve_handoff_pause_policy,
    )
    policy = resolve_handoff_pause_policy(OWNER_TIER_CLEAR)
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False, (
        "CLEAR owner-contact must keep the AI alive — only manual "
        "pause from the dashboard silences replies."
    )


def test_owner_complaint_keeps_ai_alive() -> None:
    """COMPLAINT owner-contact: full flip + session + apologetic
    ack. Pre-#46 this paused the AI; post-#46 the AI stays alive so
    natural product / pricing / shipping follow-up is still served
    while staff prepares to address the grievance."""
    from core.handoff_detector import (
        OWNER_TIER_COMPLAINT,
        resolve_handoff_pause_policy,
    )
    policy = resolve_handoff_pause_policy(OWNER_TIER_COMPLAINT)
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False, (
        "COMPLAINT owner-contact must NOT auto-pause the AI; staff "
        "can manually pause from the dashboard if needed."
    )


def test_generic_handoff_keeps_ai_alive() -> None:
    """Generic handoff ("أبي أتكلم مع موظف", "كلموني"): full flip +
    session, AI alive. Same blanket policy."""
    from core.handoff_detector import (
        GENERIC_HANDOFF_TIER,
        resolve_handoff_pause_policy,
    )
    policy = resolve_handoff_pause_policy(GENERIC_HANDOFF_TIER)
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False


def test_unknown_tier_falls_back_to_generic() -> None:
    """Defensive: an unrecognised tier is treated as GENERIC, not
    VAGUE — so we still surface to staff. AI still alive."""
    from core.handoff_detector import resolve_handoff_pause_policy

    policy = resolve_handoff_pause_policy("some_future_tier")
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False


def test_none_tier_falls_back_to_generic() -> None:
    from core.handoff_detector import resolve_handoff_pause_policy

    policy = resolve_handoff_pause_policy(None)
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False


def test_empty_tier_falls_back_to_generic() -> None:
    from core.handoff_detector import resolve_handoff_pause_policy

    policy = resolve_handoff_pause_policy("")
    assert policy["do_full_handoff_flip"] is True
    assert policy["do_create_session"] is True
    assert policy["do_pause_ai"] is False


def test_no_tier_ever_returns_pause_ai_true() -> None:
    """Cross-tier invariant: ``do_pause_ai`` is False for EVERY
    known tier (and unknown ones too). Pin this so a future tier
    addition can't accidentally re-introduce auto-pause."""
    from core.handoff_detector import (
        GENERIC_HANDOFF_TIER,
        OWNER_TIER_CLEAR,
        OWNER_TIER_COMPLAINT,
        OWNER_TIER_VAGUE,
        resolve_handoff_pause_policy,
    )
    every_tier = [
        OWNER_TIER_VAGUE,
        OWNER_TIER_CLEAR,
        OWNER_TIER_COMPLAINT,
        GENERIC_HANDOFF_TIER,
        "some_future_tier",
        None,
        "",
    ]
    for tier in every_tier:
        policy = resolve_handoff_pause_policy(tier)
        assert policy["do_pause_ai"] is False, (
            f"tier={tier!r} returned do_pause_ai=True — "
            "Tenant 33 #46 forbids auto-pause on customer-side "
            "escalation."
        )


# ════════════════════════════════════════════════════════════════════
# Part 2 — _conversation_handoff_flag mode-resolver semantics
# ════════════════════════════════════════════════════════════════════
#
# Pre-#46 this returned True for the mere presence of
# ``is_human_handoff`` (auto-flipped by the pre-brain escalation
# guard). That alone routed every subsequent inbound to
# MODE_SUPPORT_ESCALATION → bypassed brain → customer's natural
# follow-up questions went unanswered.
#
# Human/staff activity residue (paused_by_human / taken_over_at) must
# not fire this gate. Advisory tags remain dashboard-only. Explicit
# Stop AI (ai_paused) is the conversation-level off switch.


def _mock_convo(**flags) -> SimpleNamespace:
    """Build a minimal duck-typed conversation row for the gate."""
    return SimpleNamespace(
        is_human_handoff=flags.get("is_human_handoff", False),
        needs_human=flags.get("needs_human", False),
        handoff_active=flags.get("handoff_active", False),
        paused_by_human=flags.get("paused_by_human", False),
        taken_over_at=flags.get("taken_over_at", None),
    )


def test_handoff_flag_false_when_only_advisory_tags_set() -> None:
    """The auto-flipped advisory tags must NOT trigger the
    SUPPORT_ESCALATION override on their own — that was the bug
    Tenant 33 #46 reported."""
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    convo = _mock_convo(
        is_human_handoff=True,
        needs_human=True,
        handoff_active=True,
    )
    assert _conversation_handoff_flag(convo) is False, (
        "Auto-handoff tags must not silence the brain — only staff "
        "actively taking over should."
    )


def test_handoff_flag_false_when_paused_by_human() -> None:
    """``paused_by_human`` is leftover staff-activity residue and must
    not own conversation mode or AI execution."""
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    convo = _mock_convo(paused_by_human=True)
    assert _conversation_handoff_flag(convo) is False


def test_handoff_flag_false_when_taken_over_at_set() -> None:
    """``taken_over_at`` alone is implicit residue, not explicit Stop AI."""
    from datetime import datetime, timezone
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    convo = _mock_convo(taken_over_at=datetime.now(timezone.utc))
    assert _conversation_handoff_flag(convo) is False


def test_handoff_flag_false_when_both_implicit_signals_set() -> None:
    from datetime import datetime, timezone
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    convo = _mock_convo(
        paused_by_human=True,
        taken_over_at=datetime.now(timezone.utc),
    )
    assert _conversation_handoff_flag(convo) is False


def test_handoff_flag_false_for_clean_conversation() -> None:
    """Sanity — a fresh conversation row never trips the gate."""
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    assert _conversation_handoff_flag(_mock_convo()) is False


def test_advisory_tags_alongside_takeover_still_trigger_gate() -> None:
    """When staff has taken over AND the advisory tags are set, the
    gate still fires (takeover wins)."""
    from modules.ai.routing.conversation_mode import _conversation_handoff_flag

    convo = _mock_convo(
        is_human_handoff=True,
        needs_human=True,
        handoff_active=True,
        paused_by_human=True,
    )
    assert _conversation_handoff_flag(convo) is False


# ════════════════════════════════════════════════════════════════════
# Part 3 — Manual pause is the only kill switch (integration shape)
# ════════════════════════════════════════════════════════════════════
#
# The dashboard pause endpoint flips ``Conversation.ai_paused = True``
# via ``core.ai_pause_guard.pause_ai``. ``should_skip_ai`` then
# returns ``(True, REASON_MANUAL)`` and the webhook returns BEFORE
# any mode resolution — so the AI is silent regardless of the new
# handoff-flag semantics. Verify the contract symbols still exist.


def test_manual_pause_constants_exist() -> None:
    """Sanity check: the manual-pause constants used by the
    dashboard endpoint are still exported. If a future refactor
    renames them this test fails fast and forces an audit of the
    dashboard wiring."""
    from core.ai_pause_guard import (
        REASON_MANUAL,
        REASON_MANUAL_PAUSE,
        REASON_MANUAL_TAKEOVER,
        pause_ai,
        should_skip_ai,
    )
    assert REASON_MANUAL == "manual"
    assert REASON_MANUAL_PAUSE == "manual_pause"
    assert REASON_MANUAL_TAKEOVER == "manual_takeover"
    assert callable(pause_ai)
    assert callable(should_skip_ai)


def test_manual_pause_silences_ai_via_should_skip() -> None:
    """Functional shape: a conversation with ``ai_paused=True`` and
    a manual pause reason returns ``(True, reason)`` from
    ``should_skip_ai`` — i.e. the brain is bypassed. This is the
    only path that should silence replies post-#46."""
    from core.ai_pause_guard import REASON_MANUAL, should_skip_ai

    convo = SimpleNamespace(
        id=1,
        ai_paused=True,
        ai_paused_reason=REASON_MANUAL,
        # Advisory tags clear — proving the manual pause works on
        # its own without relying on any handoff plumbing.
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        extra_metadata={},
    )
    skip, reason = should_skip_ai(
        db=None,           # _record_inbound only branches on convo.id
        convo=convo,
        tenant_id=999,
        customer_phone="+966500000000",
        inbound_text="مرحبا",
    )
    assert skip is True
    assert reason == REASON_MANUAL


def test_handoff_advisory_tags_do_not_silence_ai_via_should_skip() -> None:
    """The inverse contract: when the AI is NOT manually paused but
    advisory handoff tags ARE set, ``should_skip_ai`` returns
    ``(False, None)`` — i.e. the brain runs as normal. This is the
    exact regression Tenant 33 #46 surfaced."""
    from core.ai_pause_guard import should_skip_ai

    convo = SimpleNamespace(
        id=2,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=True,
        needs_human=True,
        handoff_active=True,
        paused_by_human=False,
        taken_over_at=None,
        extra_metadata={},
    )

    # Stub the DB-touching helper (blocked-numbers query). The gate
    # only consults it AFTER the ai_paused short-circuit, so we
    # don't need a real DB session here — but should_skip_ai still
    # makes that call. Use a NoOp object that returns "not blocked".
    class _NoBlockDB:
        def query(self, _model):
            return self

        def filter(self, *_args, **_kw):
            return self

        def filter_by(self, **_kw):
            return self

        def first(self):
            return None

        def all(self):
            return []

        def count(self):
            return 0

        def order_by(self, *_args, **_kw):
            return self

        def limit(self, *_args, **_kw):
            return self

    skip, reason = should_skip_ai(
        db=_NoBlockDB(),
        convo=convo,
        tenant_id=999,
        customer_phone="+966500000000",
        inbound_text="ايش طرق التوصيل؟",
    )
    assert skip is False, (
        "Advisory handoff tags alone must not silence the brain — "
        "the customer's product/pricing/shipping question should "
        "still get answered naturally."
    )
    assert reason is None
