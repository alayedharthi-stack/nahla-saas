"""Price objection must not trigger quantity prompts or false catalog-price guards."""
from __future__ import annotations

import os
import re
import sys
from typing import Any

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_followup_policy import followup_style_for_request  # noqa: E402
from modules.ai.brain.commerce_style_compose import (  # noqa: E402
    StyleBundle,
    compose_followup_line,
    compose_personality_overlay,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    _detect_violations,
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.state.price_objection_topic import (  # noqa: E402
    build_price_objection_facts,
    detect_price_objection_topic_shift,
    should_suppress_quantity_followup,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    Intent,
    MerchantConversationState,
)

_QUANTITY_FOLLOWUP_RE = re.compile(
    r"(?:كم\s*(?:ال)?(?:كمية|عدد)|وش\s*(?:ال)?(?:كمية|عدد))",
    re.UNICODE,
)


def _ctx(message: str) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966504560698",
        message=message,
        intent=Intent(
            name=INTENT_ASK_PRICE,
            confidence=0.9,
            raw_message=message,
        ),
        state=MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
    )


def _evidence(**overrides: Any) -> ProductClaimGroundingEvidence:
    base = dict(
        grounded_prices=frozenset({250, 300}),
        grounded_text_corpus="",
        available_products=({"id": 1, "title": "عسل", "can_checkout": True},),
        unavailable_products=(),
        catalog_products_this_turn=False,
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset(),
        kb_section_ids=frozenset(),
    )
    base.update(overrides)
    return ProductClaimGroundingEvidence(**base)


def test_price_objection_does_not_trigger_quantity_prompt() -> None:
    msg = "سعره غالي يقول 250"
    assert detect_price_objection_topic_shift(msg) is True
    assert should_suppress_quantity_followup(msg) is True

    decision = DefaultDecisionEngine().decide(_ctx(msg))
    assert decision.action == ACTION_LLM_REPLY
    assert (decision.args or {}).get("topic") == "price_objection"
    facts = (decision.args or {}).get("price_objection_facts") or {}
    assert facts.get("must_not_ask_quantity_yet") is True

    style = StyleBundle(
        opening_style="warm",
        followup_style="quantity",
        emoji_style="none",
        sentence_order="fact_first",
        seed=7,
        style_signature="test|quantity|none|fact_first|7",
    )
    overlay = compose_personality_overlay(
        operational_fact="السعر يختلف حسب الكمية والتغليف.",
        style=style,
        category="general",
        emoji_pools={},
        inbound_text=msg,
    )
    assert not _QUANTITY_FOLLOWUP_RE.search(overlay)


def test_competitor_price_claim_routes_to_price_objection() -> None:
    msg = "اشتريت من عند واحد برضه ب 200"
    assert detect_price_objection_topic_shift(msg) is True

    decision = DefaultDecisionEngine().decide(_ctx(msg))
    assert decision.action == ACTION_LLM_REPLY
    assert (decision.args or {}).get("topic") == "price_objection"

    matched = intent_rules.match(msg)
    assert matched is not None
    assert matched.name == INTENT_ASK_PRICE
    assert (matched.slots or {}).get("price_objection") is True


def test_bulk_price_condition_does_not_enter_checkout_without_buy_intent() -> None:
    msg = "أخذت 50 وأفكر في 50 ثانية إذا ب 200"
    assert detect_price_objection_topic_shift(msg) is True
    assert should_suppress_quantity_followup(msg) is True

    decision = DefaultDecisionEngine().decide(_ctx(msg))
    assert decision.action == ACTION_LLM_REPLY
    assert (decision.args or {}).get("topic") == "price_objection"
    facts = build_price_objection_facts(msg)
    assert facts.get("possible_bulk_quantity") == 50
    assert facts.get("competitor_price_claim") == 200.0


def test_price_objection_preserves_catalog_price_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
    msg = "سعره غالي يقول 250"
    reply = (
        "فهمت مقارنتك بـ 200، وسعرنا في الكتالوج 250 ريال للمنتج المذكور."
    )

    def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
        return _evidence(grounded_prices=frozenset({250}))

    monkeypatch.setattr(
        "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
        _fake_build,
    )
    result = apply_product_claim_grounding_guard(
        reply=reply,
        tenant_id=33,
        inbound_metadata={"inbound_text": msg, "price_objection": True},
    )
    assert result.replaced is False
    assert result.action in ("allowed", "allowed_price_objection_customer_claims")
    assert "ما ظهر عندي سعر مؤكد" not in result.reply


def test_no_static_quantity_prompt_for_price_objection() -> None:
    msg = "عند غيركم أرخص"
    assert should_suppress_quantity_followup(msg) is True

    style_name = followup_style_for_request(
        inbound_text=msg,
        category="general",
        seeded_style="quantity",
    )
    assert style_name != "quantity"
    followup = compose_followup_line(
        StyleBundle(
            opening_style="warm",
            followup_style=style_name,
            emoji_style="none",
            sentence_order="fact_first",
            seed=1,
            style_signature="t",
        )
    )
    assert not _QUANTITY_FOLLOWUP_RE.search(followup)


def test_past_purchase_phrase_not_start_order_when_used_as_comparison() -> None:
    msg = "اشتريت من عند واحد برضه ب 200"
    matched = intent_rules.match(msg)
    assert matched is not None
    assert matched.name != INTENT_START_ORDER
    assert matched.name == INTENT_ASK_PRICE


def test_price_objection_numbers_not_treated_as_store_price_claims() -> None:
    msg = "سعره غالي يقول 250 ولقيته ب 200"
    reply = "فهمت أنك ذكرت 250 و200 للمقارنة، وسعرنا في الكتالوج 250."
    claimed = {200, 250}
    ev = _evidence(grounded_prices=frozenset({250}))
    violations = _detect_violations(reply, ev, customer_claimed=claimed)
    ungrounded = [v for v in violations if v[0] == "ungrounded_price"]
    assert not ungrounded
