"""Architecture fixes: cost gate, label hygiene, follow-up policy, style lexicon."""
from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_followup_policy import classify_commerce_request_kind  # noqa: E402
from modules.ai.brain.commerce.product_label_hygiene import is_non_product_label, sanitize_product_label  # noqa: E402
from modules.ai.brain.commerce_reply_humanizer import apply_commerce_reply_humanizer  # noqa: E402
from modules.ai.brain.commerce_style_compose import _OPENING_BY_STYLE, compose_personality_overlay, resolve_style_bundle  # noqa: E402
from modules.ai.brain.cost.model_router import (  # noqa: E402
    detect_compose_standard_signals,
    resolve_compose_model_route,
    should_block_anthropic_compose_result,
)
from modules.ai.brain.cost.model_router_audit import TIER_CHEAP  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _product_label_for_reply,
    build_operational_availability_conflict_reply,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainReplyState,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_PICK_LIST_ITEM,
    INTENT_SOLUTION_SEEKING_COMMERCE,
)
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_MODEL_ROUTER_ENABLED", "true")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP_PROVIDER", "openai_compatible")
    monkeypatch.setenv("NAHLA_MODEL_CHEAP", "gpt-4o-mini")
    monkeypatch.setenv("NAHLA_COMMERCE_PROMPT_SLIM_ENABLED", "true")


class TestCostGate:
    @pytest.mark.parametrize(
        ("intent",),
        [
            (INTENT_SOLUTION_SEEKING_COMMERCE,),
            (INTENT_ASK_PRODUCT,),
            (INTENT_ASK_PRICE,),
            (INTENT_PICK_LIST_ITEM,),
        ],
    )
    def test_routine_intents_use_cheap_no_anthropic(self, intent: str) -> None:
        route = resolve_compose_model_route(
            intent_name=intent,
            reply_state=BrainReplyState(
                store_name="x",
                intent_name=intent,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            ),
        )
        assert route.tier == TIER_CHEAP
        assert route.block_anthropic_fallback is True
        assert "anthropic" not in (route.provider_chain_override or ())
        assert should_block_anthropic_compose_result(route=route, provider_used="anthropic")

    def test_pick_list_ambiguity_does_not_upgrade_to_standard(self) -> None:
        needs, reason = detect_compose_standard_signals(
            intent_name=INTENT_PICK_LIST_ITEM,
            reply_state=BrainReplyState(
                store_name="x",
                intent_name=INTENT_PICK_LIST_ITEM,
                primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
                ambiguity_class="missing_objective",
            ),
        )
        assert needs is False
        assert reason == ""


class TestLabelHygiene:
    @pytest.mark.parametrize(
        "phrase",
        ["الخيارات", "وش الخيارات", "أرسل الخيارات أول", "كم عدد", "options"],
    )
    def test_non_product_phrases_rejected(self, phrase: str) -> None:
        assert is_non_product_label(phrase)

    def test_options_inbound_uses_focus_not_meta_label(self) -> None:
        evidence = MagicMock()
        evidence.entity.product_id = None
        evidence.entity.family_key = ""
        ctx = {"focus_product": {"title": "عسل طلح"}, "catalog_skus": []}
        label = _product_label_for_reply(
            evidence, availability_context=ctx, inbound_text="وش الخيارات؟",
        )
        assert label == "عسل طلح"


class TestFollowupPolicy:
    def test_options_request_not_quantity(self) -> None:
        assert classify_commerce_request_kind("وش الخيارات؟") == "options_list"

    def test_styled_reply_for_options_avoids_quantity(self) -> None:
        style = resolve_style_bundle(
            tenant_id=1, conversation_id=5, turn_id=2,
            intent_name=INTENT_ASK_PRODUCT, category="honey",
        )
        styled = compose_personality_overlay(
            operational_fact="متوفر عسل طلح بعدة خيارات.",
            style=style,
            category="honey",
            emoji_pools={"honey": ("🍯",), "general": ("✨",)},
            inbound_text="وش الخيارات؟",
        )
        last_line = styled.split("\n")[-1]
        assert "تحتاج" not in last_line
        assert "عدد" not in last_line


class TestStyleLexicon:
    def test_no_abdr_or_abdl_in_openers(self) -> None:
        lexicon = " ".join(o for openers in _OPENING_BY_STYLE.values() for o in openers)
        assert "أبدر" not in lexicon
        assert "أبدل" not in lexicon
