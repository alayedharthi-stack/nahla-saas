"""P0 — deterministic ACK stub removal and conversation recovery."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.persona_template_engine import (  # noqa: E402
    pick_persona_social_reply,
)
from modules.ai.brain.cost.intent_cost_policy import (  # noqa: E402
    should_avoid_llm_for_social_category,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    is_generic_ack_stub_text,
    try_guard_recovery_reply,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_WHO_ARE_YOU,
    MerchantConversationState,
)
from core.outbound_sanitizer import maybe_scrub_handoff_promise  # noqa: E402


def _ctx(msg: str, *, greeted: bool = True, history: list | None = None) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        from modules.ai.brain.types import Intent

        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        intent=intent,
        state=MerchantConversationState(greeted=greeted, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            orderable=True,
            has_active_integration=True,
            store_name="test",
        ),
        history=list(history or []),
    )


class TestGenericAckStubDetection:
    @pytest.mark.parametrize(
        "text",
        (
            "حاضر 🌷",
            "تمام 🌷 وصلت رسالتك.",
            "تم 🌷",
            "أبشر 🌷",
            "حياك الله، وصلت رسالتك.",
        ),
    )
    def test_banned_stubs_detected(self, text: str) -> None:
        assert is_generic_ack_stub_text(text)


class TestProductionConversationScenarios:
    def test_fi_albeit_not_social_ack_stub(self) -> None:
        ctx = _ctx("في البيت")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == "llm_reply"
        assert dec.args.get("topic") == "non_sales_ambiguous"
        reply = pick_persona_social_reply(ctx, "informational_only", inbound_text="في البيت")
        assert reply == ""
        assert not is_generic_ack_stub_text(reply)

    def test_ant_turki_routes_persona_identity(self) -> None:
        intent = rules.match("انت تركي")
        assert intent is not None
        assert intent.name == INTENT_WHO_ARE_YOU
        ctx = _ctx(
            "انت تركي",
            history=[
                {"direction": "in", "body": "السلام عليكم"},
                {"direction": "out", "body": "وعليكم السلام ... ياهلا ومرحباً"},
                {"direction": "in", "body": "في البيت"},
            ],
        )
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == "llm_reply"
        assert dec.args.get("topic") == "persona_identity"

    def test_abi_otlob_not_generic_receipt_stub(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="سيتواصل معك الفريق قريباً",
            inbound_text="ابي اطلب",
        )
        assert result.replaced is True
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "وصلت رسالتك" not in result.reply
        assert "منتج" in result.reply or "تطلب" in result.reply

    def test_abi_otlob_decision_is_search_not_ack(self) -> None:
        ctx = _ctx("ابي اطلب")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS


class TestConversationRecoveryLayer:
    def test_bare_start_order_recovery(self) -> None:
        rec = try_guard_recovery_reply(inbound_text="ابي اطلب")
        assert rec.reply
        assert "وصلت رسالتك" not in rec.reply
        assert not is_generic_ack_stub_text(rec.reply)

    def test_identity_probe_defers_to_persona_compose(self) -> None:
        rec = try_guard_recovery_reply(inbound_text="انت تركي")
        assert rec.needs_persona_compose is True
        assert rec.source == "persona_identity_probe"

    def test_handoff_scrub_does_not_inject_stub(self) -> None:
        out, scrubbed = maybe_scrub_handoff_promise(
            "سيتواصل معك الفريق قريباً",
            handoff_state_active=False,
        )
        assert scrubbed is True
        assert "وصلت رسالتك" not in out
        assert not is_generic_ack_stub_text(out)


class TestTemplateFirstPolicy:
    def test_informational_only_routes_to_llm(self) -> None:
        assert should_avoid_llm_for_social_category("informational_only") is False

    def test_thanks_routes_to_llm(self) -> None:
        assert should_avoid_llm_for_social_category("thanks") is False
