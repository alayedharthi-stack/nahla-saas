"""Phase 3 — evidence-based discovery presentation composer."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.catalog.catalog_intelligence import (  # noqa: E402
    CatalogGroup,
    DiscoveryPlan,
)
from modules.ai.brain.catalog.discovery_presenter import (  # noqa: E402
    DEFAULT_EMPTY_REPLY,
    DEFAULT_GUIDED_QUESTION,
    DiscoveryPresentationComposer,
    resolve_strategy_for_presentation,
)
from modules.ai.brain.catalog.presentation_contract import (  # noqa: E402
    reply_contains_ungrounded_discovery_claim,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    DiscoveryMode,
    DiscoveryStrategyResult,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import TOP_PRODUCTS  # noqa: E402
from modules.ai.brain.execution.search import _attach_discovery_presentation  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.product_discovery_gate import try_price_query_decision  # noqa: E402
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _products() -> list[dict]:
    return [
        {
            "title": "عسل طلح نجد",
            "size": "500 جم",
            "price": 120,
            "external_id": "p1",
        },
        {
            "title": "عسل سمر الحجاز",
            "size": "500 جم",
            "price": 130,
            "external_id": "p2",
        },
        {
            "title": "عسل الضحيان",
            "size": "250 جم",
            "price": 70,
            "external_id": "p3",
        },
        {
            "title": "زيت زيتون",
            "price": 45,
            "external_id": "p4",
        },
    ]


def _ctx(msg: str, *, intent_name: str | None = None) -> BrainContext:
    if intent_name is None:
        intent = rules.match(msg)
        if intent is None:
            intent = Intent(name="general", confidence=0.5, raw_message=msg)
    else:
        intent = Intent(name=intent_name, confidence=0.9, raw_message=msg)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=10,
            in_stock_count=10,
            orderable=True,
            top_products=_products(),
        ),
    )


class TestDiscoveryPresentationComposer:
    def test_products_use_real_names_and_prices(self) -> None:
        plan = DiscoveryPlan(output_kind="products", products=_products())
        strategy = DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG)
        result = DiscoveryPresentationComposer().compose(
            plan=plan,
            strategy=strategy,
        )
        assert "عسل طلح نجد" in result.text
        assert "120 ريال" in result.text
        assert "130 ريال" in result.text
        assert len(result.products) == 3

    def test_products_no_generic_ungrounded_phrase(self) -> None:
        plan = DiscoveryPlan(output_kind="products", products=_products())
        strategy = DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG)
        text = DiscoveryPresentationComposer().compose(
            plan=plan,
            strategy=strategy,
        ).text
        assert not reply_contains_ungrounded_discovery_claim(text)
        assert "ما المنتج الذي تود طلبه" not in text
        assert "عندنا أنواع مميزة" not in text

    def test_featured_header_only_for_featured_or_top_products(self) -> None:
        composer = DiscoveryPresentationComposer()
        plan = DiscoveryPlan(output_kind="products", products=_products()[:1])
        featured = composer.compose(
            plan=plan,
            strategy=DiscoveryStrategyResult(mode=DiscoveryMode.FEATURED_FIRST),
            entry_source="top_products",
        )
        neutral = composer.compose(
            plan=plan,
            strategy=DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG),
            entry_source="global_browse",
        )
        assert "الأكثر طلباً" in featured.text
        assert "الأكثر طلباً" not in neutral.text
        assert "هذه بعض الخيارات المتوفرة" in neutral.text

    def test_collections_list_real_categories_only(self) -> None:
        plan = DiscoveryPlan(
            output_kind="collections",
            collections=[
                CatalogGroup(group_id="honey", group_name="العسل", browse_rank=1),
                CatalogGroup(group_id="oils", group_name="الزيوت", browse_rank=2),
            ],
        )
        result = DiscoveryPresentationComposer().compose(
            plan=plan,
            strategy=DiscoveryStrategyResult(mode=DiscoveryMode.COLLECTIONS_FIRST),
        )
        assert "العسل" in result.text
        assert "الزيوت" in result.text
        assert "منتجات العناية" not in result.text
        assert "الأقسام المتوفرة" in result.text

    def test_guided_one_short_question(self) -> None:
        result = DiscoveryPresentationComposer().compose(
            plan=DiscoveryPlan(output_kind="guided", guided_question=""),
            strategy=DiscoveryStrategyResult(mode=DiscoveryMode.GUIDED_DISCOVERY),
        )
        assert result.text == DEFAULT_GUIDED_QUESTION
        assert result.text.count("؟") == 1
        assert len(result.text.split()) <= 10

    def test_empty_does_not_invent_products(self) -> None:
        result = DiscoveryPresentationComposer().compose(
            plan=DiscoveryPlan(output_kind="empty"),
            strategy=DiscoveryStrategyResult(mode=DiscoveryMode.DIRECT_CATALOG),
        )
        assert result.text == DEFAULT_EMPTY_REPLY
        assert "عسل" not in result.text
        assert result.output_kind == "empty"

    def test_show_more_continues_featured_presentation(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="discovery",
            last_discovery_mode=DiscoveryMode.FEATURED_FIRST.value,
        )
        strategy = resolve_strategy_for_presentation({}, state=state)
        assert strategy.mode == DiscoveryMode.FEATURED_FIRST
        payload = _attach_discovery_presentation(
            {"products": _products()[:2]},
            decision=type("D", (), {"args": {"source": "show_more"}})(),
            ctx=BrainContext(
                tenant_id=1,
                customer_phone="966500000001",
                message="وريني باقي",
                intent=Intent(name="general", confidence=0.5, raw_message="وريني باقي"),
                state=state,
                facts=CommerceFacts(has_products=True),
            ),
            source="show_more",
        )
        assert "الأكثر طلباً" in payload["discovery_presentation_text"]


class TestDiscoveryPresentationBypasses:
    def test_price_ask_bypasses_discovery_search(self) -> None:
        msg = "كم سعر الكيلو؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="ask_price", confidence=0.9, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        decision = try_price_query_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CLARIFY
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_identity_bypasses_discovery_search(self) -> None:
        dec = DefaultDecisionEngine().decide(_ctx("من انت"))
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "persona_identity"
        assert dec.action != ACTION_SEARCH_PRODUCTS

    def test_active_order_bypasses_discovery_search(self) -> None:
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            order_status="awaiting_address",
        )
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message="https://maps.app.goo.gl/abc123test",
            intent=Intent(name="general", confidence=0.55, raw_message="maps"),
            state=MerchantConversationState(
                stage=STAGE_ORDERING,
                greeted=True,
                order_prep=prep,
            ),
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action != ACTION_SEARCH_PRODUCTS

    def test_top_products_entry_stamps_discovery_entry_type(self) -> None:
        dec = DefaultDecisionEngine().decide(_ctx("الاكثر طلبا"))
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("discovery_entry_type") == TOP_PRODUCTS
