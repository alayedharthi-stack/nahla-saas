"""Phase 2 clear-intent LLM recompose + follow-up fixes."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
def _enable_clear_intent(monkeypatch):
    monkeypatch.setenv("CLEAR_INTENT_FALLBACK_NET_ENABLED", "true")
    monkeypatch.setenv("CLEAR_INTENT_RECOMPOSE_ENABLED", "true")
    monkeypatch.setenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", "true")


class TestClearIntentRecompose:
    def test_clear_intent_facts_trigger_llm_recompose(self, monkeypatch):
        from core.clear_intent_recompose import maybe_recompose_clear_intent_reply
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        ci = apply_clear_intent_fallback_net(
            customer_msg="هل يوجد عروض على العسل",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        assert ci.fired is True

        llm_reply = "أكيد، أقدر أعرض لك العروض المتوفرة حالياً 🌷"

        async def _run():
            with patch(
                "core.constrained_operational_compose.compose_constrained_operational_reply",
                new=AsyncMock(
                    return_value=(
                        llm_reply,
                        {"copy_source": "constrained_compose", "reply_source": "constrained_compose"},
                    )
                ),
            ) as mock_compose:
                text, meta = await maybe_recompose_clear_intent_reply(
                    db=MagicMock(),
                    tenant_id=1,
                    phone="966500000000",
                    clear_intent_result=ci,
                    inbound_text="هل يوجد عروض على العسل",
                    weak_reply=PRODUCTION_TIMEOUT_REPLY,
                )
            assert mock_compose.await_count == 1
            assert text == llm_reply
            assert meta.get("recomposed") is True
            assert text != PRODUCTION_TIMEOUT_REPLY

        asyncio.run(_run())

    def test_clear_intent_recompose_does_not_use_static_template(self):
        from core.clear_intent_recompose import build_clear_intent_instruction_from_result
        from modules.ai.postprocess import safety_nets
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        assert not hasattr(safety_nets, "_CLEAR_INTENT_REPLIES")

        ci = apply_clear_intent_fallback_net(
            customer_msg="بكم سعر العسل؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        instr = build_clear_intent_instruction_from_result(
            ci,
            weak_reply=PRODUCTION_TIMEOUT_REPLY,
            inbound_text="بكم سعر العسل؟",
        )
        assert instr.legacy_copy == PRODUCTION_TIMEOUT_REPLY
        assert instr.facts.get("detected_intent") == "price"
        assert "أبشر" not in instr.legacy_copy or PRODUCTION_TIMEOUT_REPLY.startswith("عذر")

    def test_clear_intent_required_delivery_preserved(self):
        from core.clear_intent_recompose import build_clear_intent_instruction_from_result
        from modules.ai.brain.compose.operational_expression import (
            compose_operational_expression_goal,
        )
        from modules.ai.postprocess.safety_nets import apply_clear_intent_fallback_net

        ci = apply_clear_intent_fallback_net(
            customer_msg="كم رسوم الشحن؟",
            reply_text=PRODUCTION_TIMEOUT_REPLY,
        )
        instr = build_clear_intent_instruction_from_result(
            ci,
            weak_reply=PRODUCTION_TIMEOUT_REPLY,
            inbound_text="كم رسوم الشحن؟",
        )
        assert instr.facts.get("required_delivery") == "llm_rephrase"
        goal = compose_operational_expression_goal(instr)
        assert "llm_rephrase" in goal
        assert "shipping" in instr.facts.get("detected_intent", "") or "shipping" in goal.lower()

    def test_clear_intent_skips_recompose_when_not_fired(self):
        from core.clear_intent_recompose import maybe_recompose_clear_intent_reply
        from modules.ai.postprocess.safety_nets import (
            ClearIntentFallbackResult,
        )

        async def _run():
            text, meta = await maybe_recompose_clear_intent_reply(
                db=MagicMock(),
                tenant_id=1,
                phone="966500000000",
                clear_intent_result=ClearIntentFallbackResult(),
                inbound_text="مرحبا",
                weak_reply="مرحبا بك",
            )
            assert text == "مرحبا بك"
            assert meta.get("recomposed") is False

        asyncio.run(_run())


class TestOrderResumeHintMetadataPaths:
    def _active_ctx(self):
        from modules.ai.brain.types import (
            BrainContext,
            CommerceFacts,
            Intent,
            MerchantConversationState,
        )

        state = MerchantConversationState()
        state.order_prep.product_id = "1"
        state.order_prep.missing_fields = ["city"]
        state.current_product_focus = {"title": "عسل", "id": "1"}
        return BrainContext(
            tenant_id=1,
            customer_phone="966500000000",
            message="?",
            intent=Intent(name="track_order", confidence=0.9),
            state=state,
            facts=CommerceFacts(),
            history=[],
        )

    def test_order_resume_hint_metadata_preserved_for_track_coupon_addon_paths(self):
        from modules.ai.brain.compose.responder import DefaultComposer

        composer = DefaultComposer()
        ctx = self._active_ctx()
        result = SimpleNamespace(data={})

        for body in (
            "حالة الطلب",
            "كوبون خصم",
            "منتج إضافي",
        ):
            result.data = {}
            out = composer._with_follow_up(body, ctx, result=result)
            assert out == body
            assert "نكمل" not in out
            hint = result.data.get("order_resume_hint") or {}
            assert hint.get("active_order_context") is True
            assert hint.get("pending_slot") == "city"


class TestHandoffPromiseGuard:
    def test_handoff_promise_guard_blocks_output_without_evidence(self):
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

    def test_handoff_promise_guard_allows_with_evidence(self):
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

        with_evidence = choose_safe_fallback(
            "أبي أتكلم مع موظف",
            reason=FALLBACK_REASON_BRAIN_EXCEPTION,
            store_has_live_agent=False,
            escalation_evidence_ok=True,
        )
        assert with_evidence.metadata.get("escalation_evidence_ok") is True
        assert with_evidence.response_goal == GOAL_HANDOFF
