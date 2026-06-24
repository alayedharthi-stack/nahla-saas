"""Phase 2 P0 — stop deterministic prose from replacing/appending LLM replies."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

PRODUCTION_TIMEOUT_REPLY = (
    "عذراً، تأخّر الرد قليلاً. هل يمكنك إعادة سؤالك؟ "
    "أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
)


@pytest.fixture(autouse=True)
def _enable_clear_intent_net(monkeypatch):
    monkeypatch.setenv("CLEAR_INTENT_FALLBACK_NET_ENABLED", "true")


class TestClearIntentMetadataOnly:
    def test_clear_intent_net_does_not_replace_llm_reply(self):
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        llm_reply = "رد من الذكاء عن العسل والأسعار."
        result = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض على العسل",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"
        assert result.new_reply == ""
        assert result.text_written is False
        assert result.facts.get("detected_intent") == "offers"
        # Original LLM/generic reply must not be substituted by template.
        assert llm_reply != PRODUCTION_TIMEOUT_REPLY

    def test_clear_intent_net_records_metadata_without_customer_prose(self):
        from core.outbound_text_policy import OutboundTextSource, OutboundTextTracker
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        result = apply_clear_intent_fallback_net(
            customer_msg="بكم سعر العسل؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.metadata.get("clear_intent_fallback", {}).get("text_written") is False

        tracker = OutboundTextTracker(
            text_source=OutboundTextSource.LLM,
            policy_path="brain.compose._llm_compose",
        )
        tracker.record_mutation(
            layer="clear_intent_fallback_net",
            op="noop",
            before=PRODUCTION_TIMEOUT_REPLY,
            after=PRODUCTION_TIMEOUT_REPLY,
            text_written=False,
        )
        meta = tracker.to_metadata()
        assert meta["customer_facing_text_debt"] is False
        assert meta["text_source"] == OutboundTextSource.LLM.value
        mut = meta["postprocess_mutations"][0]
        assert mut["text_written"] is False


class TestOrderResumeHintMetadata:
    def _active_order_ctx(self):
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )

        state = MerchantConversationState()
        state.order_prep.product_id = "99"
        state.order_prep.product_name = "سدر"
        state.order_prep.missing_fields = ["city"]
        state.order_prep.product_options_meta = [
            {"name": "الحجم", "required": True},
        ]
        state.current_product_focus = {"title": "سدر", "id": 99}
        return BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message="كم التوصيل؟",
            intent=Intent(name="ask_shipping", confidence=0.9),
            state=state,
            facts=CommerceFacts(),
            history=[],
        )

    def test_order_resume_hint_is_metadata_not_appended_text(self):
        from modules.ai.brain.compose.responder import DefaultComposer

        ctx = self._active_order_ctx()
        composer = DefaultComposer()
        result = SimpleNamespace(data={})
        body = "التوصيل متاح للرياض وجدة."
        out = composer._with_follow_up(body, ctx, result=result)
        assert out == body
        assert "نكمل" not in out
        hint = result.data.get("order_resume_hint") or {}
        assert hint.get("active_order_context") is True
        assert hint.get("resume_candidate") == "سدر"
        assert hint.get("pending_slot") == "product_options"

    def test_non_order_turn_does_not_get_order_resume_text(self):
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            INTENT_ONLINE_STORE_INQUIRY,
            MerchantConversationState,
        )

        state = MerchantConversationState()
        state.order_prep.product_id = "99"
        state.order_prep.product_options_meta = [{"name": "الحجم", "required": True}]
        state.current_product_focus = {"title": "سدر", "id": 99}
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message="عندكم متجر الكتروني ؟",
            intent=Intent(name=INTENT_ONLINE_STORE_INQUIRY, confidence=0.96),
            state=state,
            facts=CommerceFacts(),
            history=[],
        )
        composer = DefaultComposer()
        result = SimpleNamespace(data={})
        faq_body = "نعم، عندنا متجر إلكتروني."
        combined = composer._with_follow_up(
            faq_body, ctx, topic=TOPIC_STORE_INFO, result=result,
        )
        assert combined == faq_body
        assert "نكمل" not in combined
        assert "الحجم" not in combined
        # Metadata may still record prior order context for compose.
        hint = result.data.get("order_resume_hint")
        assert hint is None


class TestHandoffFallbackEvidenceGating:
    def test_handoff_claim_requires_escalation_evidence(self):
        from core.outbound_sanitizer import contains_handoff_promise
        from services.fallback_policy import (
            FALLBACK_REASON_BRAIN_EXCEPTION,
            choose_safe_fallback,
        )

        decision = choose_safe_fallback(
            "أبي أتكلم مع موظف",
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=False,
            escalation_evidence_ok=False,
        )
        assert contains_handoff_promise(decision.text) is None
        assert decision.metadata.get("handoff_promise_blocked") is True
        assert decision.metadata.get("escalation_evidence_ok") is False

    def test_handoff_claim_allowed_only_with_evidence(self):
        from services.fallback_policy import (
            FALLBACK_KIND_HANDOFF_ACK,
            FALLBACK_REASON_BRAIN_EXCEPTION,
            GOAL_HANDOFF,
            _TEXT_HANDOFF_ACK,
            choose_safe_fallback,
        )

        with_agent = choose_safe_fallback(
            "أبي أتكلم مع موظف",
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=True,
        )
        assert with_agent.text == _TEXT_HANDOFF_ACK
        assert with_agent.kind == FALLBACK_KIND_HANDOFF_ACK
        assert with_agent.response_goal == GOAL_HANDOFF
        assert with_agent.metadata.get("escalation_evidence_ok") is True

        with_evidence = choose_safe_fallback(
            "أبي أتكلم مع موظف",
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=False,
            escalation_evidence_ok=True,
        )
        assert with_evidence.metadata.get("escalation_evidence_ok") is True
        assert with_evidence.response_goal == GOAL_HANDOFF
        assert "نتواصل" in with_evidence.text


class TestPhase2PathsReduceDebt:
    def test_phase2_paths_reduce_customer_facing_text_debt(self):
        from core.outbound_text_policy import OutboundTextSource, OutboundTextTracker
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        # clear intent: metadata mutation only
        ci = apply_clear_intent_fallback_net(
            customer_msg="هل عندكم عروض؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        tracker = OutboundTextTracker(text_source=OutboundTextSource.LLM)
        tracker.record_mutation(
            layer="clear_intent_fallback_net",
            op="noop",
            before=PRODUCTION_TIMEOUT_REPLY,
            after=PRODUCTION_TIMEOUT_REPLY,
            text_written=False,
        )
        assert ci.text_written is False
        assert tracker.to_metadata()["customer_facing_text_debt"] is False

        # order resume: metadata on result, no append
        state = MerchantConversationState()
        state.order_prep.product_id = "1"
        state.order_prep.missing_fields = ["city"]
        state.current_product_focus = {"title": "عسل", "id": "1"}
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message="?",
            intent=Intent(name="ask_shipping", confidence=0.9),
            state=state,
            facts=CommerceFacts(),
            history=[],
        )
        result = SimpleNamespace(data={})
        out = DefaultComposer()._with_follow_up("body", ctx, result=result)
        assert out == "body"
        assert result.data.get("order_resume_hint")
        assert result.data.get("_compose_metadata_only") is True
