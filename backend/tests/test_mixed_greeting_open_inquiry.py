"""Mixed greeting + open category inquiry — platform-wide P1/P2 regression."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.greeting_etiquette import (  # noqa: E402
    apply_greeting_etiquette,
    detect_salam_level,
    reply_already_has_salam_return,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    inbound_exempt_from_availability_rewrite,
    should_block_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    INQUIRY_CLASS_OPEN,
    classify_product_inquiry_route,
    is_open_category_inquiry_turn,
    try_broad_category_inquiry_decision,
)
from modules.ai.brain.social_human_context import compute_social_human_context  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

_UNKNOWN_SNIPPET = "ما نقدر نؤكد التوفر"
_BROWSE_ACTIONS = {ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE}


def _ctx(
    message: str,
    *,
    intent_name: str = "ask_product",
    slots: dict | None = None,
    greeted: bool = True,
) -> BrainContext:
    slot_data = dict(slots or {})
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        raw_message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.86,
            raw_message=message,
            slots=slot_data,
        ),
        state=MerchantConversationState(greeted=greeted, stage="discovery"),
        facts=CommerceFacts(has_products=True, orderable=True),
    )


def _open_inquiry_decision(msg: str) -> object:
    ctx = _ctx(msg, slots={"embedded_greeting": "السلام عليكم" in msg})
    decision = DefaultDecisionEngine().decide(ctx)
    return decision


class TestOpenInquiryRouting:
    @pytest.mark.parametrize(
        "msg",
        [
            "السلام عليكم، أبغى الاستفسار عن العطر",
            "هلا، عندي سؤال عن الأحذية",
            "مساء الخير، أبغى أعرف عن الساعات",
            "السلام عليكم، عندي استفسار عن العسل",
            "أبغى الاستفسار عن العطور",
        ],
    )
    def test_open_inquiry_routes_llm_not_search(self, msg: str) -> None:
        decision = _open_inquiry_decision(msg)
        assert decision.action == ACTION_LLM_REPLY, msg
        assert decision.action != ACTION_SEARCH_PRODUCTS, msg
        assert decision.args.get("topic") == "open_category_inquiry", msg
        assert decision.args.get("block_availability_rewrite") is True, msg
        assert decision.args.get("category_scope"), msg

    def test_classify_open_inquiry(self) -> None:
        msg = "أبغى الاستفسار عن العطر"
        inquiry_class, route = classify_product_inquiry_route(_ctx(msg), query="العطر")
        assert inquiry_class == INQUIRY_CLASS_OPEN
        assert route == "llm"

    def test_explicit_browse_still_searches(self) -> None:
        msg = "وش أنواع العطور عندكم؟"
        decision = DefaultDecisionEngine().decide(_ctx(msg))
        assert decision.action in _BROWSE_ACTIONS
        assert not is_open_category_inquiry_turn(msg, "العطور")

    def test_explicit_availability_not_open_inquiry(self) -> None:
        msg = "عندكم عطر ورد؟"
        assert not is_open_category_inquiry_turn(msg, "عطر ورد")
        decision = DefaultDecisionEngine().decide(_ctx(msg))
        assert decision.action != ACTION_LLM_REPLY or (
            decision.args.get("topic") != "open_category_inquiry"
        )

    def test_mixed_greeting_explicit_availability_not_open(self) -> None:
        msg = "السلام عليكم، عندكم عطر ورد؟"
        assert not is_open_category_inquiry_turn(msg, "عطر ورد")
        decision = DefaultDecisionEngine().decide(
            _ctx(msg, slots={"embedded_greeting": True}),
        )
        assert decision.args.get("topic") != "open_category_inquiry"

    def test_specific_price_unchanged(self) -> None:
        msg = "كم سعر عطر ورد 100 مل؟"
        decision = DefaultDecisionEngine().decide(_ctx(msg, intent_name="ask_price"))
        assert decision.action != ACTION_LLM_REPLY or (
            decision.args.get("topic") != "open_category_inquiry"
        )

    def test_playful_not_open_inquiry(self) -> None:
        msg = "وش عندك من سوالف؟"
        assert not is_open_category_inquiry_turn(msg, "")
        decision = DefaultDecisionEngine().decide(_ctx(msg, intent_name="social"))
        assert decision.args.get("topic") != "open_category_inquiry"


class TestGreetingPreservation:
    def test_embedded_greeting_does_not_suppress_prepend(self) -> None:
        msg = "السلام عليكم، أبغى الاستفسار عن العطر"
        intent = Intent(
            name="ask_product",
            confidence=0.9,
            raw_message=msg,
            slots={"embedded_greeting": True},
        )
        shc = compute_social_human_context(
            message=msg,
            intent=intent,
            state=MerchantConversationState(stage="discovery"),
        )
        assert shc.suppress_embedded_greeting_prepend is False

    def test_mixed_turn_prepends_salam_when_missing(self) -> None:
        msg = "السلام عليكم، أبغى الاستفسار عن العطر"
        body = "تفضّل، كيف أقدر أساعدك؟"
        out = apply_greeting_etiquette(body, msg, state=MerchantConversationState())
        assert detect_salam_level(msg)
        assert reply_already_has_salam_return(out)
        assert body in out
        assert out.count("وعليكم السلام") == 1

    def test_no_duplicate_salam_when_already_present(self) -> None:
        msg = "السلام عليكم، أبغى الاستفسار عن العطر"
        body = "وعليكم السلام ورحمة الله وبركاته\nتفضّل"
        out = apply_greeting_etiquette(body, msg, state=MerchantConversationState())
        assert out.count("وعليكم السلام") == 1

    def test_greeting_only_unchanged(self) -> None:
        msg = "السلام عليكم"
        decision = DefaultDecisionEngine().decide(
            _ctx(msg, intent_name="greeting", greeted=False),
        )
        assert decision.action in {ACTION_GREET, ACTION_LLM_REPLY}


class TestAvailabilityGuardOpenInquiry:
    def test_open_inquiry_exempt_from_rewrite(self) -> None:
        msg = "أبغى الاستفسار عن العطور"
        assert inbound_exempt_from_availability_rewrite(msg) is True
        assert should_block_availability_rewrite(
            inbound_text=msg,
            evidence_state="unknown",
            guard_action="rewrite_unknown",
        )

    def test_guard_does_not_emit_unknown_for_open_inquiry(self) -> None:
        msg = "أبغى الاستفسار عن العطور"
        invented = "متوفر عطر ورد بعدة خيارات."
        result = apply_product_availability_truth_guard(
            reply=invented,
            inbound_text=msg,
            tenant_id=99,
        )
        assert _UNKNOWN_SNIPPET not in (result.reply or "")
        assert result.reply != _UNKNOWN_REPLY_AR

    def test_explicit_availability_still_subject_to_guard(self) -> None:
        msg = "عندكم عطر ورد؟"
        assert not inbound_exempt_from_availability_rewrite(msg)


class TestCategoryAnchorStamp:
    def test_open_inquiry_stamps_category_scope(self) -> None:
        msg = "أبغى الاستفسار عن الساعات"
        ctx = _ctx(msg)
        decision = try_broad_category_inquiry_decision(ctx, query="الساعات")
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert ctx.state.last_browse_query in {"ساعات", "الساعات"}
