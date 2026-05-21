"""tests/test_human_priority_mode.py
────────────────────────────────────
Coverage for the Human-Priority Mode flag added behind
``NAHLA_AI_HUMAN_PRIORITY_MODE=1``. The mode lets the AI keep answering
informational questions and reassuring the customer AFTER a handoff
request is raised, but clamps every sales-/payment-/coupon-related
action — and falls back to the legacy hard-stop the moment a human
actually engages.

We test the moving parts in isolation so a regression at any single
seam is caught locally:

  1. ``ai_pause_guard.should_skip_ai`` — feature-flag gating + the
     human_priority vs human_active split driven by
     ``_is_human_actually_active``.
  2. ``RealPolicyGate._human_priority_clamp`` — aggressive actions get
     downgraded, informational actions keep flowing but get tagged so
     the composer can append reassurance.

The tests deliberately do NOT spin up the full BrainPipeline — that's
covered elsewhere — and use lightweight stand-ins for ``Conversation``
/ ``MessageEvent`` / ``Session`` so they stay fast and DB-free.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── Fixture: in-memory stand-ins for the SA tables touched by the guard ──────


class _FakeQuery:
    """Tiny chainable that mimics enough of the SA Query interface for
    ``_is_human_actually_active`` to walk the outbound history."""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        return _FakeQuery(self._rows[: int(n)])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """``db.query(Model).filter(...).first()`` returns the FIRST row of
    whatever list we stuffed under ``Model.__name__`` at construction
    time. That's all ``_is_human_actually_active`` needs."""

    def __init__(self, **tables):
        self._tables = {k: list(v) for k, v in tables.items()}

    def query(self, model):
        rows = self._tables.get(getattr(model, "__name__", str(model)), [])
        return _FakeQuery(rows)


def _convo(*, ai_paused=True, reason="human_handoff", taken_over_at=None, convo_id=11):
    """Build a fake Conversation row with the columns the guard reads."""
    return SimpleNamespace(
        id=convo_id,
        tenant_id=1,
        ai_paused=ai_paused,
        ai_paused_reason=reason,
        taken_over_at=taken_over_at,
        extra_metadata={},
    )


def _msg(*, event_type="ai_reply", seconds_ago=0, is_ai=True):
    return SimpleNamespace(
        id=100 - seconds_ago,
        tenant_id=1,
        conversation_id=11,
        direction="outbound",
        event_type=event_type,
        body="...",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        extra_metadata={"is_ai": is_ai},
    )


# ── ai_pause_guard.should_skip_ai ────────────────────────────────────────────


class TestShouldSkipAiHumanPriority:
    """The pre-LLM gate is the single point that decides whether AI runs
    at all this turn. We assert the new branching behaviour with the
    feature flag both ON and OFF."""

    def test_flag_off_keeps_legacy_hard_stop(self, monkeypatch):
        """When the flag is unset (default), a handoff pause MUST still
        return ``(True, REASON_HUMAN_HANDOFF)`` so existing tenants see
        zero behaviour change."""
        # Force the module-level constant to False to mimic env unset.
        import core.ai_pause_guard as guard

        monkeypatch.setattr(guard, "HUMAN_PRIORITY_MODE_ENABLED", False)

        from models import MessageEvent  # noqa: PLC0415
        db = _FakeSession(MessageEvent=[])
        convo = _convo(reason=guard.REASON_HUMAN_HANDOFF, taken_over_at=None)

        skip, reason = guard.should_skip_ai(
            db, convo,
            tenant_id=convo.tenant_id,
            customer_phone="966500000001",
            inbound_text="مرحبا",
        )
        assert skip is True
        assert reason == guard.REASON_HUMAN_HANDOFF

    def test_flag_on_no_human_active_returns_human_priority(self, monkeypatch):
        """Flag ON + handoff pause + nobody picked up yet → AI keeps
        running with the synthetic ``human_priority`` reason."""
        import core.ai_pause_guard as guard

        monkeypatch.setattr(guard, "HUMAN_PRIORITY_MODE_ENABLED", True)

        from models import MessageEvent  # noqa: PLC0415
        # Only AI outbounds in history — staff hasn't typed yet.
        db = _FakeSession(MessageEvent=[_msg(event_type="ai_reply", seconds_ago=10)])
        convo = _convo(reason=guard.REASON_HUMAN_HANDOFF, taken_over_at=None)

        skip, reason = guard.should_skip_ai(
            db, convo,
            tenant_id=convo.tenant_id,
            customer_phone="966500000001",
            inbound_text="ابي اطمن على وضعي",
        )
        assert skip is False
        assert reason == guard.REASON_HUMAN_PRIORITY

    def test_flag_on_taken_over_at_set_hard_stops(self, monkeypatch):
        """Flag ON but ``taken_over_at`` is stamped (staff clicked the
        takeover button) → AI must go fully silent. This is the
        legacy hard-stop path preserved for the "human is engaging" case."""
        import core.ai_pause_guard as guard

        monkeypatch.setattr(guard, "HUMAN_PRIORITY_MODE_ENABLED", True)

        from models import MessageEvent  # noqa: PLC0415
        db = _FakeSession(MessageEvent=[])
        convo = _convo(
            reason=guard.REASON_MANUAL_TAKEOVER,
            taken_over_at=datetime.now(timezone.utc),
        )

        skip, reason = guard.should_skip_ai(
            db, convo,
            tenant_id=convo.tenant_id,
            customer_phone="966500000001",
            inbound_text="موجود؟",
        )
        assert skip is True
        assert reason == guard.REASON_MANUAL_TAKEOVER

    def test_flag_on_recent_manual_outbound_hard_stops(self, monkeypatch):
        """Flag ON but the most recent outbound is a manual reply within
        the look-back window → AI must defer to the agent."""
        import core.ai_pause_guard as guard

        monkeypatch.setattr(guard, "HUMAN_PRIORITY_MODE_ENABLED", True)

        from models import MessageEvent  # noqa: PLC0415
        db = _FakeSession(MessageEvent=[
            _msg(event_type="manual_reply", seconds_ago=5, is_ai=False),
        ])
        convo = _convo(reason=guard.REASON_HUMAN_HANDOFF, taken_over_at=None)

        skip, reason = guard.should_skip_ai(
            db, convo,
            tenant_id=convo.tenant_id,
            customer_phone="966500000001",
            inbound_text="هل تم تجهيز طلبي؟",
        )
        assert skip is True
        assert reason == guard.REASON_HUMAN_HANDOFF

    def test_flag_on_non_handoff_pause_unchanged(self, monkeypatch):
        """Flag ON but the pause is for a non-human reason (e.g. bot loop
        or internal number) → behaviour must NOT change. Human-Priority
        Mode is strictly scoped to ``HUMAN_PRESENCE_REASONS``."""
        import core.ai_pause_guard as guard

        monkeypatch.setattr(guard, "HUMAN_PRIORITY_MODE_ENABLED", True)

        from models import MessageEvent  # noqa: PLC0415
        db = _FakeSession(MessageEvent=[])
        convo = _convo(reason=guard.REASON_BOT_LOOP, taken_over_at=None)

        skip, reason = guard.should_skip_ai(
            db, convo,
            tenant_id=convo.tenant_id,
            customer_phone="966500000001",
            inbound_text="مرحبا",
        )
        assert skip is True
        assert reason == guard.REASON_BOT_LOOP


# ── RealPolicyGate._human_priority_clamp ─────────────────────────────────────


class _StubFacts:
    store_name = ""
    assistant_name = ""
    within_working_hours = True


def _ctx(*, human_priority: bool):
    """Build a minimal BrainContext-compatible namespace. The clamp only
    touches ``human_priority``, ``tenant_id``, ``customer_phone`` so we
    don't need the full BrainContext machinery here."""
    return SimpleNamespace(
        tenant_id=1,
        customer_phone="966500000001",
        human_priority=human_priority,
        merchant_context={},
        facts=_StubFacts(),
        intent=SimpleNamespace(name="GENERAL"),
        state=SimpleNamespace(greeted=True, stage="active"),
    )


class TestHumanPriorityClamp:
    """Clamp behaviour table — the spec says aggressive actions get
    downgraded, informational ones get tagged. We assert both branches
    + the no-op when the flag isn't set."""

    @pytest.fixture
    def gate(self):
        from modules.ai.brain.decision.policy import RealPolicyGate
        return RealPolicyGate()

    def test_no_op_when_flag_false(self, gate):
        from modules.ai.brain.decision.actions import ACTION_SEND_PAYMENT_LINK
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_SEND_PAYMENT_LINK, args={"link": "x"})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=False))

        assert d_out.action == ACTION_SEND_PAYMENT_LINK
        assert "human_priority" not in (d_out.args or {})

    def test_blocks_send_payment_link(self, gate):
        from modules.ai.brain.decision.actions import (
            ACTION_LLM_REPLY, ACTION_SEND_PAYMENT_LINK,
        )
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_SEND_PAYMENT_LINK, args={"link": "x"})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))

        assert d_out.action == ACTION_LLM_REPLY
        assert d_out.args.get("human_priority") is True
        assert "clamp original=send_payment_link" in d_out.reason

    def test_blocks_propose_draft_order(self, gate):
        from modules.ai.brain.decision.actions import (
            ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER,
        )
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_PROPOSE_DRAFT_ORDER, args={})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))

        assert d_out.action == ACTION_LLM_REPLY
        assert d_out.args.get("human_priority") is True

    def test_blocks_suggest_coupon(self, gate):
        from modules.ai.brain.decision.actions import (
            ACTION_LLM_REPLY, ACTION_SUGGEST_COUPON,
        )
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_SUGGEST_COUPON, args={})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))

        assert d_out.action == ACTION_LLM_REPLY
        assert d_out.args.get("human_priority") is True

    def test_blocks_recommend_addon(self, gate):
        from modules.ai.brain.decision.actions import (
            ACTION_LLM_REPLY, ACTION_RECOMMEND_ADDON,
        )
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_RECOMMEND_ADDON, args={})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))

        assert d_out.action == ACTION_LLM_REPLY
        assert d_out.args.get("human_priority") is True

    def test_passes_informational_actions(self, gate):
        """FAQ / search / clarify / greet / llm_reply MUST keep their
        original action — they're informational and safe to deliver
        while the human is being notified."""
        from modules.ai.brain.decision.actions import (
            ACTION_FAQ_REPLY, ACTION_SEARCH_PRODUCTS, ACTION_CLARIFY,
            ACTION_GREET, ACTION_LLM_REPLY, ACTION_HANDOFF,
        )
        from modules.ai.brain.types import Decision

        for safe_action in (
            ACTION_FAQ_REPLY, ACTION_SEARCH_PRODUCTS, ACTION_CLARIFY,
            ACTION_GREET, ACTION_LLM_REPLY, ACTION_HANDOFF,
        ):
            d_in = Decision(action=safe_action, args={})
            d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))
            assert d_out.action == safe_action, f"action {safe_action} was rewritten"
            assert d_out.args.get("human_priority") is True, (
                f"action {safe_action} did not pick up the human_priority tag"
            )

    def test_preserves_existing_args(self, gate):
        """The clamp must NOT eat caller args — payment link clamping
        still strips the link (we downgrade the whole action) but FAQ
        clamping must preserve fields like ``topic``."""
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY
        from modules.ai.brain.types import Decision

        d_in = Decision(action=ACTION_FAQ_REPLY, args={"topic": "shipping", "x": 1})
        d_out = gate._human_priority_clamp(d_in, _ctx(human_priority=True))

        assert d_out.args.get("topic") == "shipping"
        assert d_out.args.get("x") == 1
        assert d_out.args.get("human_priority") is True
