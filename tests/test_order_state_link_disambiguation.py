"""
tests/test_order_state_link_disambiguation.py
──────────────────────────────────────────────
Regression coverage for order-state-aware link handling: after an
order is confirmed, tracking/shipping link asks must NOT restart
checkout or inject the store URL.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


ORDER_CONFIRMED_HISTORY = [
    {
        "direction": "outbound",
        "body": (
            "طلبك رقم 262511443 تم تأكيده وهو الآن بانتظار المراجعة 🌷"
        ),
    },
    {
        "direction": "inbound",
        "body": "رابط التتبع",
    },
    {
        "direction": "outbound",
        "body": (
            "للأسف ما يتوفر عندي رابط تتبع حالياً لأن الطلب لسه بمرحلة "
            "المراجعة... بمجرد ما يتم شحنه ويصدر رقم التتبع بنرسله لك "
            "مباشرة بإذن الله."
        ),
    },
]


def _make_state(**kwargs):
    from modules.ai.brain.types import MerchantConversationState, OrderPreparationState

    prep_kwargs = {}
    state_kwargs = {}
    for k, v in kwargs.items():
        if hasattr(OrderPreparationState, k):
            prep_kwargs[k] = v
        else:
            state_kwargs[k] = v
    state = MerchantConversationState(**state_kwargs)
    if prep_kwargs:
        state.order_prep = OrderPreparationState(**prep_kwargs)
    return state


class TestLinkDisambiguationHelpers:
    @pytest.mark.parametrize(
        "msg",
        [
            "رابط التتبع",
            "تمام بس تشحنو ارسلو الرابط",
            "إذا شحنتوا ارسلوا الرابط",
            "أرسلوا رقم التتبع",
            "متى يوصلني رابط الشحن",
        ],
    )
    def test_tracking_phrases_with_post_order_history(self, msg):
        from modules.ai.brain.intent.link_disambiguation import (
            looks_like_tracking_link_request,
        )

        assert looks_like_tracking_link_request(
            msg,
            history=ORDER_CONFIRMED_HISTORY,
        ) is True

    def test_store_link_without_post_order_stays_store(self):
        from modules.ai.brain.intent.link_disambiguation import (
            looks_like_store_link_request,
            looks_like_tracking_link_request,
        )

        msg = "رابط المتجر"
        assert looks_like_store_link_request(msg) is True
        assert looks_like_tracking_link_request(msg) is False

    def test_payment_link_not_misclassified_as_tracking(self):
        from modules.ai.brain.intent.link_disambiguation import (
            looks_like_payment_link_request,
            looks_like_tracking_link_request,
        )

        msg = "رابط الدفع"
        assert looks_like_payment_link_request(msg) is True
        assert looks_like_tracking_link_request(
            msg,
            history=ORDER_CONFIRMED_HISTORY,
        ) is False


class TestIntentRules:
    def test_confirmed_order_tracking_link_classifies_as_track_order(self):
        from modules.ai.brain.intent.rules import match
        from modules.ai.brain.types import INTENT_TRACK_ORDER

        result = match("تمام بس تشحنو ارسلو الرابط")
        assert result is not None
        assert result.name == INTENT_TRACK_ORDER

    def test_store_link_before_order_still_store_info(self):
        from modules.ai.brain.intent.rules import match
        from modules.ai.brain.types import INTENT_ASK_STORE_INFO

        result = match("رابط المتجر")
        assert result is not None
        assert result.name == INTENT_ASK_STORE_INFO


class TestStoreLinkSafetyNetGating:
    def _patch_store_url(self, monkeypatch, url):
        from modules.ai.postprocess import safety_nets
        monkeypatch.setattr(
            safety_nets,
            "_lookup_tenant_store_url",
            lambda db, tenant_id: url,
        )

    def test_post_order_tracking_ask_does_not_inject_store_url(
        self, monkeypatch,
    ):
        from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

        self._patch_store_url(monkeypatch, "https://ayedhoney.com")
        bad_reply = (
            "تمام 🌷 الشحن مجاني على العسل\n"
            "أقدر أجهز طلبك هنا مباشرة...\nhttps://ayedhoney.com"
        )
        res = apply_store_link_safety_net(
            MagicMock(),
            tenant_id=33,
            customer_msg="تمام بس تشحنو ارسلو الرابط",
            reply_text=bad_reply,
            history=ORDER_CONFIRMED_HISTORY,
        )
        assert res.fired is False
        assert res.skipped_reason == "no_store_link_intent"

    def test_pre_order_store_link_still_injects(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

        self._patch_store_url(monkeypatch, "https://ayedhoney.com")
        res = apply_store_link_safety_net(
            MagicMock(),
            tenant_id=33,
            customer_msg="ارسل الرابط",
            reply_text="هذا متجرنا 🌷",
            history=[],
        )
        assert res.fired is True
        assert "https://ayedhoney.com" in res.new_reply


class TestDecisionEnginePostOrder:
    def _ctx(self, message, intent_name, state, history=None):
        from modules.ai.brain.types import BrainContext, CommerceFacts, Intent

        return BrainContext(
            tenant_id=33,
            customer_phone="+966500000001",
            message=message,
            intent=Intent(
                name=intent_name,
                confidence=0.96,
                raw_message=message,
            ),
            state=state,
            facts=CommerceFacts(orderable=True),
            history=history or ORDER_CONFIRMED_HISTORY,
        )

    def test_tracking_follow_up_does_not_force_propose_draft_order(self):
        from modules.ai.brain.decision.actions import (
            ACTION_LLM_REPLY,
            ACTION_PROPOSE_DRAFT_ORDER,
        )
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.types import INTENT_TRACK_ORDER
        from modules.ai.brain.state.stages import STAGE_ORDERING

        state = _make_state(
            stage=STAGE_ORDERING,
            order_status="under_review",
            current_product_focus={
                "id": 1,
                "external_id": "ext-1",
                "title": "عسل سدر",
            },
        )
        eng = DefaultDecisionEngine()
        d = eng.decide(
            self._ctx(
                "تمام بس تشحنو ارسلو الرابط",
                INTENT_TRACK_ORDER,
                state,
            )
        )
        assert d.action == ACTION_LLM_REPLY
        assert d.action != ACTION_PROPOSE_DRAFT_ORDER
        assert d.args.get("topic") == "tracking_link_follow_up"
        assert d.args.get("tracking_available") is False
        assert d.args.get("order_reference") == "262511443"

    def test_tracking_link_routes_to_generative_llm_without_tracking_url(self):
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.types import INTENT_TRACK_ORDER

        eng = DefaultDecisionEngine()
        d = eng.decide(
            self._ctx(
                "رابط التتبع",
                INTENT_TRACK_ORDER,
                _make_state(order_status="under_review"),
            )
        )
        assert d.action == ACTION_LLM_REPLY
        assert d.args.get("topic") == "tracking_link_follow_up"
        assert d.args.get("tracking_available") is False

    def test_post_order_context_survives_product_questions_in_between(self):
        from modules.ai.brain.intent.link_disambiguation import (
            has_active_post_order_context,
            should_use_generative_tracking_follow_up,
        )

        history = list(ORDER_CONFIRMED_HISTORY)
        for _ in range(6):
            history.extend([
                {"direction": "inbound", "body": "كم سعر ارخص عسل عندكم"},
                {
                    "direction": "outbound",
                    "body": "عندنا عسل سدر بـ 180 ريال للكيلو 🌷",
                },
            ])
        assert has_active_post_order_context(history=history) is True
        assert should_use_generative_tracking_follow_up(
            "تمام بس تشحنو ارسلو الرابط",
            history=history,
        ) is True

    def test_normal_product_question_unaffected(self):
        from modules.ai.brain.intent.link_disambiguation import (
            looks_like_tracking_link_request,
            should_use_generative_tracking_follow_up,
        )

        assert looks_like_tracking_link_request("كم سعر العسل") is False
        assert should_use_generative_tracking_follow_up(
            "كم سعر العسل",
            history=ORDER_CONFIRMED_HISTORY,
        ) is False

    def test_unpaid_payment_link_still_pay_now(self):
        from modules.ai.brain.decision.actions import ACTION_SEND_PAYMENT_LINK
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.types import INTENT_PAY_NOW
        from modules.ai.brain.state.stages import STAGE_CHECKOUT

        state = _make_state(
            stage=STAGE_CHECKOUT,
            order_status="payment_pending",
            checkout_url="https://pay.example/checkout/123",
        )
        eng = DefaultDecisionEngine()
        d = eng.decide(
            self._ctx(
                "رابط الدفع",
                INTENT_PAY_NOW,
                state,
                history=[],
            )
        )
        assert d.action == ACTION_SEND_PAYMENT_LINK


class TestTrackingFollowUpResponseGoal:
    def test_response_goal_includes_order_context_and_generative_directive(self):
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import Decision, SuggestionSnapshot

        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "tracking_link_follow_up",
                "order_reference": "262511443",
                "order_status": "under_review",
                "tracking_available": False,
            },
            reason="post-order tracking follow-up",
        )
        goal = _compose_base_response_goal(decision, SuggestionSnapshot())
        assert "tracking_link_follow_up" in goal
        assert "262511443" in goal
        assert "under_review" in goal
        assert "tracking_available=false" in goal
        assert "store_url" in goal
        assert "Do NOT stay silent" in goal
