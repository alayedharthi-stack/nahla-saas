"""tests/test_handoff_during_order.py
─────────────────────────────────
Coverage for the May 2026 handoff-during-order policy change:

  * Expanded INTENT_TALK_HUMAN detection — broader Saudi/Gulf dialect
    phrases like "حولني" / "كلموني" / "أبي مختص" must classify as a
    talk-to-human request.
  * ``DefaultDecisionEngine.decide`` emits ``ACTION_HANDOFF`` even
    when the customer has an active order in flight (previously the
    engine silently dropped the handoff in favour of order recovery).
  * ``RealPolicyGate._working_hours`` keeps the action as
    ``ACTION_HANDOFF`` outside working hours, but tags
    ``args["after_hours"]=True`` so the responder ships the polite
    off-hours copy AND the webhook still raises the handoff session
    + needs_human / handoff_active flags.
  * The responder honours the ``after_hours`` flag.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ── Intent detection ──────────────────────────────────────────────────────


class TestExpandedHandoffPhrases:
    """Each phrase below was reported in production by merchants whose
    customers were being absorbed into the AI flow instead of being
    routed to a human. We assert that the rule fires on every variant."""

    def test_expanded_phrases_classify_as_talk_human(self):
        from modules.ai.brain.intent.rules import match
        from modules.ai.brain.types import INTENT_TALK_HUMAN

        for phrase in (
            "أبي موظف",
            "أبغى موظف",
            "كلموني",
            "كلميني",
            "حولني لموظف",
            "حوّلني للدعم",
            "أبي أكلم أحد",
            "أبغى أكلم موظف",
            "أبي مختص",
            "أحتاج موظف بشري",
            "في أحد يرد؟",
            "فيه أحد يرد",
            "هل في موظف",
            "talk to a human",
            "transfer me to support",
        ):
            intent = match(phrase)
            assert intent is not None, f"{phrase!r} produced no intent"
            assert intent.name == INTENT_TALK_HUMAN, (
                f"{phrase!r} expected TALK_HUMAN, got {intent.name}"
            )


# ── DecisionEngine: handoff during active order ───────────────────────────


def _facts(within_working_hours: bool = True):
    from modules.ai.brain.types import CommerceFacts
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        has_active_integration=True,
        orderable=True,
        has_coupons=False,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        store_url="https://store.example.com",
        within_working_hours=within_working_hours,
    )


def _make_state(stage: str, *, product=None):
    from modules.ai.brain.types import MerchantConversationState
    state = MerchantConversationState()
    state.stage = stage
    state.greeted = True
    state.current_product_focus = product
    return state


def _ctx(state, intent_name: str, *, message: str = "حولني لموظف",
         within_working_hours: bool = True):
    from modules.ai.brain.types import BrainContext, Intent
    intent = Intent(name=intent_name, confidence=0.92, slots={})
    return BrainContext(
        tenant_id=7,
        customer_phone="+966555555555",
        customer_id=42,
        message=message,
        history=[],
        profile={},
        intent=intent,
        state=state,
        facts=_facts(within_working_hours=within_working_hours),
    )


class TestHandoffEmittedDuringActiveOrder:
    """The customer typed "حولني لموظف" mid-order. The engine MUST
    emit ACTION_HANDOFF so the webhook can pin the conversation to a
    human, even though there's a product focus + order_prep state.
    Previously the engine silently dropped the handoff in favour of
    the order-recovery branch — that's exactly what we're fixing."""

    def test_handoff_emitted_without_active_order(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        from modules.ai.brain.state.stages import STAGE_DISCOVERY
        from modules.ai.brain.types import INTENT_TALK_HUMAN

        engine = DefaultDecisionEngine()
        state = _make_state(STAGE_DISCOVERY, product=None)
        decision = engine.decide(_ctx(state, INTENT_TALK_HUMAN))
        assert decision.action == ACTION_HANDOFF
        # No "during_active_order" marker because no order was pending.
        assert not (decision.args or {}).get("during_active_order")

    def test_handoff_emitted_during_active_order(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        from modules.ai.brain.state.stages import STAGE_ORDERING
        from modules.ai.brain.types import INTENT_TALK_HUMAN

        engine = DefaultDecisionEngine()
        state = _make_state(STAGE_ORDERING, product={"id": 1, "title": "فستان"})
        # Simulate an active order_prep so the previous-block code path
        # would have dropped the handoff in favour of the order
        # propose-draft branch.
        state.order_prep = {"product_id": 1, "product_name": "فستان"}

        decision = engine.decide(_ctx(state, INTENT_TALK_HUMAN))
        assert decision.action == ACTION_HANDOFF, (
            "the customer's explicit handoff request must be honoured "
            "even mid-order — previously this was dropped to "
            "ACTION_PROPOSE_DRAFT_ORDER"
        )
        # The engine tags the args so the webhook can audit-log
        # "customer bailed mid-cart".
        assert (decision.args or {}).get("during_active_order") is True


# ── PolicyGate: after-hours handoff ───────────────────────────────────────


class TestAfterHoursHandoff:
    """The policy gate used to silently downgrade off-hours HANDOFF →
    LLM_REPLY, which meant the merchant never saw the inbox red-pill.
    We now keep ACTION_HANDOFF + tag ``after_hours=True`` so the
    responder ships the polite "team will reply during work hours"
    copy AND the webhook persists the handoff session + flags."""

    def _gate_decide(self, *, within_working_hours: bool):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.policy import RealPolicyGate
        from modules.ai.brain.state.stages import STAGE_DISCOVERY
        from modules.ai.brain.types import INTENT_TALK_HUMAN

        engine = DefaultDecisionEngine()
        state = _make_state(STAGE_DISCOVERY, product=None)
        ctx = _ctx(
            state, INTENT_TALK_HUMAN,
            within_working_hours=within_working_hours,
        )
        decision = engine.decide(ctx)
        return RealPolicyGate().gate(decision, ctx), ctx

    def test_during_working_hours_passes_through_handoff(self):
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        decision, _ = self._gate_decide(within_working_hours=True)
        assert decision.action == ACTION_HANDOFF
        assert not (decision.args or {}).get("after_hours")

    def test_outside_working_hours_keeps_handoff_and_marks_after_hours(self):
        from modules.ai.brain.decision.actions import ACTION_HANDOFF
        decision, _ = self._gate_decide(within_working_hours=False)
        # The policy KEEPS the action so the webhook still creates the
        # handoff session + raises needs_human / handoff_active.
        # Previously this was downgraded to ACTION_LLM_REPLY, which
        # lost the inbox signal.
        assert decision.action == ACTION_HANDOFF
        args = decision.args or {}
        assert args.get("after_hours") is True
        assert args.get("policy_reason") == "outside_working_hours_handoff"


# ── Responder: after_hours template ───────────────────────────────────────


class TestHandoffResponseCopy:
    def test_after_hours_template_used_when_flag_set(self):
        from modules.ai.brain.compose import templates as T
        text = T.handoff_after_hours()
        assert "خارج أوقات الدوام" in text or "الدوام" in text
        assert "موظف" in text or "الفريق" in text
        # The off-hours copy should NOT promise an immediate reply —
        # the customer needs to know the team will respond LATER.
        assert "الآن" not in text or "بإذن الله" in text

    def test_regular_handoff_copy_unchanged(self):
        from modules.ai.brain.compose import templates as T
        for variant in range(3):
            text = T.handoff(variant=variant)
            assert text  # non-empty
            # The regular handoff variants imply imminent contact
            # (within work hours) — they MUST NOT claim "outside work
            # hours" since that's the after-hours template's job.
            assert "خارج أوقات الدوام" not in text
