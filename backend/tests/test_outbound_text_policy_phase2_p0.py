"""Phase 2 P0 — stop deterministic prose from overriding LLM replies."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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
def _enable_safety_nets(monkeypatch):
    monkeypatch.setenv("CLEAR_INTENT_FALLBACK_NET_ENABLED", "true")
    monkeypatch.setenv("DELIVERY_INFO_CONTEXT_NET_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_REASK_GUARD_ENABLED", "true")
    monkeypatch.setenv("STORE_LINK_NET_ENABLED", "true")
    yield


class TestClearIntentNetDoesNotReplaceLlmReply:
    def test_clear_intent_net_does_not_replace_llm_reply(self):
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        result = apply_clear_intent_fallback_net(
            customer_msg="سلام عليكم هل يوجد عروض على العسل",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert result.fired is True
        assert result.customer_intent == "offers"
        assert not (result.new_reply or "").strip()
        assert result.facts_patch.get("clear_intent_resolved") == "offers"
        assert result.facts_patch.get("needs_recompose") is True


class TestOrderResumeHintMetadata:
    def test_order_resume_hint_is_metadata_not_appended_text(self):
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.execution.faq import TOPIC_SHIPPING
        from modules.ai.brain.types import (
            ActionResult,
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
            OrderPreparationState,
        )

        prep = OrderPreparationState(product_id="p1")
        prep.product_options_meta = [{"name": "الحجم", "required": True}]
        state = MerchantConversationState(
            greeted=True,
            current_product_focus={"id": "p1", "title": "عسل سدر"},
            order_prep=prep,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000000",
            message="كم التوصيل؟",
            intent=Intent(name="ask_shipping", confidence=0.9),
            state=state,
            facts=CommerceFacts(has_products=True),
        )
        result = ActionResult(success=True, data={})
        composer = DefaultComposer()
        faq_text = "بالنسبة للشحن: توصيل متاح"
        out = composer._with_follow_up(faq_text, ctx, topic=TOPIC_SHIPPING, result=result)
        assert out == faq_text
        assert "نكمل" not in out
        assert "وش الحجم" not in out
        meta = result.data.get("order_resume_metadata") or {}
        assert meta.get("resume_candidate") is True
        assert meta.get("pending_options") == ["الحجم"]

    def test_non_order_turn_does_not_get_order_resume_text(self):
        from modules.ai.brain.compose.responder import DefaultComposer
        from modules.ai.brain.execution.faq import TOPIC_STORE_INFO
        from modules.ai.brain.types import (
            ActionResult,
            BrainContext,
            CommerceFacts,
            Intent,
            INTENT_ONLINE_STORE_INQUIRY,
            MerchantConversationState,
            OrderPreparationState,
        )

        prep = OrderPreparationState(product_id="99")
        prep.product_options_meta = [{"name": "الحجم", "required": True}]
        state = MerchantConversationState(
            order_prep=prep,
            current_product_focus={"title": "سدر", "id": 99},
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message="عندكم متجر الكتروني ؟",
            intent=Intent(name=INTENT_ONLINE_STORE_INQUIRY, confidence=0.96),
            state=state,
            facts=CommerceFacts(has_products=True),
        )
        result = ActionResult(success=True, data={})
        out = DefaultComposer()._with_follow_up(
            "هذا رابط المتجر",
            ctx,
            topic=TOPIC_STORE_INFO,
            result=result,
        )
        assert "وش الحجم" not in out
        assert "نكمل" not in out
        assert "order_resume_metadata" not in result.data


class TestHandoffFallbackEvidenceGated:
    def test_handoff_fallback_requires_escalation_evidence_for_human_promise(self):
        from services.fallback_policy import choose_safe_fallback, FALLBACK_REASON_BRAIN_EXCEPTION

        handoff_msg = "ابي اكلم موظف"
        without_evidence = choose_safe_fallback(
            handoff_msg,
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=True,
            has_escalation_evidence=False,
        )
        assert "فريق المتجر" not in without_evidence.text
        assert "سيتواصل" not in without_evidence.text
        assert "يتواصل معك" not in without_evidence.text

        with_evidence = choose_safe_fallback(
            handoff_msg,
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=True,
            has_escalation_evidence=True,
        )
        assert "فريق" in with_evidence.text or "يتواصل" in with_evidence.text

    def test_no_api_key_fallback_has_no_human_promise(self):
        from services.fallback_policy import (
            FALLBACK_REASON_NO_API_KEY,
            choose_safe_fallback,
        )

        decision = choose_safe_fallback(
            "كم السعر؟",
            reason=FALLBACK_REASON_NO_API_KEY,
        )
        assert "فريق المتجر" not in decision.text
        assert "سيتواصل" not in decision.text


class TestSafetyNetP0PathsNoCustomerProse:
    def test_store_link_no_url_writes_facts_not_prose(self, monkeypatch):
        from modules.ai.postprocess.safety_nets import apply_store_link_safety_net

        monkeypatch.setattr(
            "modules.ai.postprocess.safety_nets._lookup_tenant_store_url",
            lambda db, tenant_id: "",
        )
        res = apply_store_link_safety_net(
            MagicMock(),
            tenant_id=11,
            customer_msg="رابط المتجر",
            reply_text="هذا متجرنا 🌷",
        )
        assert res.fired is True
        assert res.rewrote_reply is False
        assert not (res.new_reply or "").strip()
        assert res.facts_patch.get("store_url_resolved") is False

    def test_delivery_net_writes_facts_not_ack_template(self):
        from modules.ai.postprocess.safety_nets import apply_delivery_info_context_net

        history = [
            {"direction": "in", "body": "إيصال"},
            {
                "direction": "out",
                "body": "ممكن ترسل لي عنوان الشحن أو المدينة عشان نرتب لك التوصيل؟",
            },
        ]
        result = apply_delivery_info_context_net(
            customer_msg="خالد\n0552375813\nالمدينة المنورة\nحي الصناعية",
            reply_text="أعتذر، هذا خارج تخصصي.",
            history=history,
        )
        assert result.fired is True
        assert not (result.new_reply or "").strip()
        assert result.facts_patch.get("delivery_info_received") is True

    def test_product_reask_guard_strips_without_template_ack(self):
        from modules.ai.postprocess.safety_nets import (
            apply_product_reask_guard,
            strip_product_reask_prose,
        )

        bad = "قبل ما نكمل، اختر المنتج اللي تبغاه من القائمة"
        stripped = strip_product_reask_prose(bad)
        assert "اختر المنتج" not in stripped
        assert not stripped.strip() or stripped != bad


class TestOutboundTextDebtDecreases:
    def test_outbound_text_debt_decreases_for_phase2_paths(self):
        from core.outbound_text_policy import OutboundTextTracker, OutboundTextSource
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        tracker = OutboundTextTracker(
            text_source=OutboundTextSource.LLM,
            policy_path="brain.compose._llm_compose",
        )
        result = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        tracker.record_facts_patch(
            layer="clear_intent_fallback_net",
            facts_patch=result.facts_patch,
            before=PRODUCTION_TIMEOUT_REPLY,
            after=PRODUCTION_TIMEOUT_REPLY,
        )
        meta = tracker.to_metadata()
        assert meta["text_source"] == "llm"
        assert meta["customer_facing_text_debt"] is False
        mut = meta["postprocess_mutations"][0]
        assert mut["text_written"] is False
        assert mut["layer"] == "clear_intent_fallback_net"
