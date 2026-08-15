"""Human-acceptance platform recovery — broad semantic-family suite.

Asserts truth availability, provenance, capability grounding, and
topic-switch continuity. Does not assert exact customer-facing prose.
Does not add phrase routers for هدية / آخر طلب / ارسل صور / فرعكم.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.store_knowledge import build_merchant_context  # noqa: E402
from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    stamp_assistant_named_catalog_from_reply,
)
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    try_catalog_navigation_decision,
)
from modules.ai.brain.commerce.catalog_reasoning_evidence import (  # noqa: E402
    catalog_reasoning_titles,
    collect_catalog_reasoning_candidates,
)
from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.customer_order_evidence import (  # noqa: E402
    collect_customer_order_evidence,
)
from modules.ai.brain.commerce.fact_answer import (  # noqa: E402
    KIND_BRANCH_EXISTENCE,
    KIND_LOCATION,
    KIND_PAYMENT_METHODS,
    KIND_RETURN_POLICY,
    KIND_SHIPPING_ETA,
    KIND_SHIPPING_FEE,
    STATUS_UNKNOWN,
    build_fact_answer_contract,
    catalog_must_yield_to_fact_owner,
    classify_fact_answer,
)
from modules.ai.brain.commerce.visual_delivery_capability import (  # noqa: E402
    collect_visual_delivery_capability,
    try_visual_catalog_send_decision,
    visual_delivery_available,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_LOCATION,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRODUCT,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_TRACK_ORDER,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_product,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_SHOE = "حذاء رياضي أبيض"
GENERIC_SHIRT = "قميص قطني أزرق"
GENERIC_PERFUME = "عطر ورد 100ml"
GENERIC_CUSTOMER = "أحمد سالم"
STORE_URL = "https://shop.example.test/demo-store"

BROWSE_FAMILY = ("وش عندكم؟", "وريني شي مناسب", "طيب نرجع للتسوق وريني شي مناسب")
RECOMMEND_FAMILY = ("وش تنصحيني فيه؟", "محتار وش آخذ")
GIFT_FOLLOWUP_FAMILY = ("طيب نرجع للهدية، وش تنصحيني فيه؟", "هدية مناسبة وش في؟")
MEDIA_FAMILY = ("نعم ارسل صور", "ارسل صور", "وريني الصور")
CURRENT_ORDER_FAMILY = ("وين طلبي الحالي؟", "وين طلبي؟")
PREVIOUS_ORDER_FAMILY = ("وش طلباتي السابقة؟", "الطلب اللي طلبته منكم قبل، وش كان؟")
ORDER_CONTENTS_FAMILY = ("وش المنتجات اللي كانت في طلبي؟", "وش كان داخل طلبي؟")
PAYMENTS_FAMILY = ("وش طرق الدفع", "كيف أدفع؟")
LOCATION_FAMILY = ("وفرعكم وين؟", "وين موقعكم؟")
SHIPPING_FAMILY = ("كم تكلفة الشحن؟", "كم يستغرق الشحن؟")
STORE_LINK_FAMILY = ("ارسل رابط المتجر", "وين رابط المتجر؟")
SOCIAL_FAMILY = ("السلام عليكم", "يسعدك", "تسلم")


def _facts(*, perfume: bool = False, images: bool = True) -> CommerceFacts:
    rows: List[Dict[str, Any]] = [
        {
            "id": 11,
            "title": GENERIC_SHOE,
            "in_stock": True,
            "can_checkout": True,
            "external_id": "ext-shoe",
            "image_url": "https://cdn.example.test/shoe.jpg" if images else "",
        },
        {
            "id": 12,
            "title": GENERIC_SHIRT,
            "in_stock": True,
            "can_checkout": True,
            "external_id": "ext-shirt",
            "image_url": "https://cdn.example.test/shirt.jpg" if images else "",
        },
    ]
    if perfume:
        rows.append(
            {
                "id": 13,
                "title": GENERIC_PERFUME,
                "in_stock": True,
                "can_checkout": True,
                "external_id": "ext-perfume",
                "image_url": "https://cdn.example.test/perfume.jpg" if images else "",
            }
        )
    return CommerceFacts(
        store_name=GENERIC_MERCHANT,
        store_url=STORE_URL,
        store_url_resolved=True,
        store_url_source="salla_store_info.domain",
        has_products=True,
        product_count=len(rows),
        in_stock_count=len(rows),
        orderable=True,
        discovery_products=rows,
        top_products=rows,
        payment_methods=["cod", "bank", "mahally"],
        shipping_methods=["Dev Company"],
        merchant_capabilities={
            "source": "salla",
            "kind": "merchant_enabled",
            "payments": {
                "status": "known",
                "methods": [{"code": "cod", "enabled": True}],
            },
            "shipping": {
                "companies_status": "known",
                "companies": [{"id": 1, "name": "Dev Company", "enabled": True}],
            },
        },
    )


def _intent(message: str, name: str) -> Intent:
    return Intent(
        name=name,
        confidence=0.9,
        slots={},
        raw_message=message,
        extraction_method="slot_extractor",
    )


def _ctx(
    message: str,
    *,
    intent_name: str,
    facts: CommerceFacts | None = None,
    state: MerchantConversationState | None = None,
    history: List[Dict[str, str]] | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone=DEFAULT_PHONE_E164,
        message=message,
        intent=_intent(message, intent_name),
        state=state or MerchantConversationState(stage="exploring", turn=4, greeted=True),
        facts=facts or _facts(),
        history=history or [],
        profile={"inbound_metadata": {}},
        commerce_bundle={},
    )


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


class TestCatalogBrowseAndRecommendation:
    @pytest.mark.parametrize("message", BROWSE_FAMILY + RECOMMEND_FAMILY + GIFT_FOLLOWUP_FAMILY)
    def test_bounded_real_catalog_is_available(self, message: str) -> None:
        titles = catalog_reasoning_titles(facts=_facts())
        assert GENERIC_SHOE in titles
        assert GENERIC_SHIRT in titles
        assert GENERIC_PERFUME not in titles
        assert 1 < len(titles) <= 8

    def test_perfume_title_is_available_only_when_in_catalog(self) -> None:
        without = catalog_reasoning_titles(facts=_facts(perfume=False))
        with_perfume = catalog_reasoning_titles(facts=_facts(perfume=True))
        assert GENERIC_PERFUME not in without
        assert GENERIC_PERFUME in with_perfume


class TestOrderTrackingThenCatalogNavigate:
    def test_catalog_navigate_clears_order_tracking_focus(self) -> None:
        from modules.ai.brain.commerce.commerce_focus_owner import (
            FOCUS_ORDER_TRACKING,
            apply_commerce_focus_lifecycle,
            set_product_focus,
        )

        state = MerchantConversationState(stage="exploring", turn=6, greeted=True)
        set_product_focus(
            state,
            {"id": 12, "title": GENERIC_SHIRT, "external_id": "ext-shirt"},
            reason="browse",
            turn=4,
        )
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_TRACK_ORDER,
            action="track_order",
            message="وين طلبي الحالي؟",
            turn=5,
        )
        assert state.conversation_focus == FOCUS_ORDER_TRACKING
        apply_commerce_focus_lifecycle(
            state,
            intent_name="start_order",
            action="catalog_navigate",
            message="طيب نرجع للتسوق",
            turn=6,
        )
        assert state.conversation_focus != FOCUS_ORDER_TRACKING

    def test_shopping_llm_reply_clears_order_tracking_focus(self) -> None:
        from modules.ai.brain.commerce.commerce_focus_owner import (
            FOCUS_ORDER_TRACKING,
            apply_commerce_focus_lifecycle,
        )

        state = MerchantConversationState(stage="exploring", turn=8, greeted=True)
        state.conversation_focus = FOCUS_ORDER_TRACKING
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_ASK_PRODUCT,
            action="llm_reply",
            message="طيب نرجع للتسوق",
            turn=8,
        )
        assert state.conversation_focus != FOCUS_ORDER_TRACKING


class TestCatalogGroundingMixedRecommend:
    def test_strips_ungrounded_family_keeps_grounded_title(self) -> None:
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"
        result = apply_catalog_product_grounding_guard(
            reply="فستان جميل أو عطر مميز",
            inbound_text="طيب نرجع للهدية، وش تنصحيني فيه؟",
            inbound_metadata={"catalog_reasoning_titles": ["فستان", GENERIC_SHIRT]},
        )
        assert result.replaced is True
        assert "عطر" not in result.reply
        assert "فستان" in result.reply

    def test_allows_perfume_when_catalog_contains_it(self) -> None:
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"
        result = apply_catalog_product_grounding_guard(
            reply="فستان جميل أو عطر مميز",
            inbound_text="طيب نرجع للهدية، وش تنصحيني فيه؟",
            inbound_metadata={
                "catalog_reasoning_titles": ["فستان سهرة", GENERIC_PERFUME],
            },
        )
        assert result.replaced is False
        assert "عطر" in result.reply

    def test_payment_or_phrase_is_not_treated_as_product_list(self) -> None:
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"
        reply = "تقدر تدفع كاش أو تحويل بنكي"
        result = apply_catalog_product_grounding_guard(
            reply=reply,
            inbound_text="وش طرق الدفع",
            inbound_metadata={"catalog_reasoning_titles": [GENERIC_SHIRT]},
        )
        assert result.replaced is False
        assert result.reply == reply


class TestCatalogDoesNotStealOrderOrVisual:
    @pytest.mark.parametrize(
        ("message", "intent_name"),
        [
            ("وين طلبي الحالي؟", INTENT_TRACK_ORDER),
            ("وش المنتجات اللي كانت في طلبي؟", INTENT_TRACK_ORDER),
            ("آخر طلب لي وش كان فيه؟", INTENT_LATEST_ORDER_SUMMARY),
            ("نعم ارسل صور", INTENT_PRODUCT_VISUAL_REQUEST),
        ],
    )
    def test_catalog_owners_yield(self, message: str, intent_name: str) -> None:
        assert catalog_must_yield_to_fact_owner(
            intent_name=intent_name,
            message=message,
        ) is True
        ctx = _ctx(message, intent_name=intent_name)
        assert try_commerce_entry_catalog_decision(ctx) is None
        assert try_catalog_navigation_decision(ctx) is None


class TestOrderFollowupContinuity:
    def test_referenced_cancelled_line_items_stay_visible(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        conv = seed_conversation(db, tenant.id, customer_id=customer.id)
        open_order = seed_order(
            db,
            tenant.id,
            source="salla",
            status="in_progress",
            external_id="257404293",
            external_order_number="257404293",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"product_title": GENERIC_SHOE, "quantity": 1}],
            extra_metadata={"created_at": "2026-05-04T10:00:00+00:00"},
        )
        open_order.customer_id = customer.id
        cancelled = seed_order(
            db,
            tenant.id,
            source="salla",
            status="cancelled",
            external_id="269977976",
            external_order_number="269977976",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"product_title": "تنورة", "quantity": 1}],
            extra_metadata={"created_at": "2026-07-02T10:00:00+00:00"},
        )
        cancelled.customer_id = customer.id
        db.commit()
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=tenant.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer.id,
            conversation_id=conv.id,
            last_discussed_order_ref="269977976",
        )
        assert payload is not None
        assert payload["roles"]["latest_order"] == "newest_including_cancelled"
        assert payload["referenced_order"]["display_reference"] == "269977976"
        names = {item["name"] for item in payload["referenced_order"]["line_items"]}
        assert "تنورة" in names
        assert payload["current_open_order"]["display_reference"] == "257404293"
        prev_refs = {row["display_reference"] for row in payload["previous_orders"]}
        assert "269977976" in prev_refs
        assert "257404293" not in prev_refs


class TestProductMediaCapability:
    def test_visual_capability_from_catalog_images(self) -> None:
        cap = collect_visual_delivery_capability(facts=_facts(images=True))
        assert visual_delivery_available(cap) is True
        assert cap["products"][0]["image_url"]

    def test_visual_capability_absent_without_images(self) -> None:
        cap = collect_visual_delivery_capability(facts=_facts(images=False))
        assert visual_delivery_available(cap) is False

    def test_visual_ask_executes_when_capability_exists(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=5, greeted=True)
        state.last_presented_products = list(_facts().discovery_products)
        ctx = _ctx(
            "نعم ارسل صور",
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("after_search") == "product_visual"
        assert decision.args.get("force_product_card") is True
        assert decision.args.get("replay_candidates")
        assert decision.action != ACTION_TRACK_ORDER

    def test_visual_helper_returns_search_when_imageable(self) -> None:
        ctx = _ctx("نعم ارسل صور", intent_name=INTENT_PRODUCT_VISUAL_REQUEST)
        decision = try_visual_catalog_send_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS


class TestActionPromiseGrounding:
    def test_staff_fetch_promise_blocked_without_handoff_evidence(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="ما عندي معلومات مؤكدة. سأطلب من فريق المتجر تزويدك بالموقع الصحيح.",
            inbound_text="وفرعكم وين؟",
            conversation_flags={},
        )
        assert result.replaced is True
        assert "سأطلب من فريق" not in result.reply


class TestMerchantFacts:
    @pytest.mark.parametrize("message", PAYMENTS_FAMILY)
    def test_payment_methods_are_known(self, message: str) -> None:
        req = classify_fact_answer(message, intent_name=INTENT_ASK_PAYMENT_INFO)
        assert req is not None
        assert req.fact_kind == KIND_PAYMENT_METHODS
        contract = build_fact_answer_contract(req, facts=_facts())
        methods = list(contract.claimable_values or []) or list(
            (contract.known_facts or {}).get("payment_methods") or []
        )
        assert methods or contract.status != STATUS_UNKNOWN

    @pytest.mark.parametrize("message", LOCATION_FAMILY)
    def test_missing_branch_is_unknown(self, message: str) -> None:
        req = classify_fact_answer(message, intent_name=INTENT_ASK_LOCATION)
        assert req is not None
        assert req.fact_kind in {KIND_LOCATION, KIND_BRANCH_EXISTENCE}
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN

    @pytest.mark.parametrize("message", SHIPPING_FAMILY)
    def test_shipping_fee_and_eta_unknown_without_evidence(self, message: str) -> None:
        req = classify_fact_answer(message)
        assert req is not None
        assert req.fact_kind in {KIND_SHIPPING_FEE, KIND_SHIPPING_ETA}
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN

    def test_store_url_is_authoritative_fact(self) -> None:
        facts = _facts()
        assert facts.store_url == STORE_URL
        assert facts.store_url_resolved is True
        assert facts.store_url_source != "none"

    def test_return_policy_unknown_without_source(self) -> None:
        req = classify_fact_answer("وش سياسة الاسترجاع؟")
        assert req is not None
        assert req.fact_kind == KIND_RETURN_POLICY
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN


class TestLatencySkipCatalogScan:
    def test_include_products_false_skips_product_list(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        seed_product(db, tenant.id, title=GENERIC_SHOE, external_id="ext-shoe")
        seed_product(db, tenant.id, title=GENERIC_SHIRT, external_id="ext-shirt")
        with_products = build_merchant_context(db, tenant.id, include_products=True)
        without = build_merchant_context(db, tenant.id, include_products=False)
        assert list(with_products.get("products") or [])
        assert list(without.get("products") or []) == []

    def test_catalog_candidate_cache_reuses_first_collect(self) -> None:
        merchant_context: Dict[str, Any] = {}
        first = collect_catalog_reasoning_candidates(
            facts=_facts(),
            merchant_context=merchant_context,
        )
        cached = merchant_context.get("_catalog_reasoning_candidates")
        assert cached
        second = collect_catalog_reasoning_candidates(
            facts=None,
            merchant_context=merchant_context,
        )
        assert [row["title"] for row in first] == [row["title"] for row in second]


class TestTopicSwitchingLongConversation:
    def test_truth_surfaces_remain_available_across_topics(self, db) -> None:
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        conv = seed_conversation(db, tenant.id, customer_id=customer.id)
        open_order = seed_order(
            db,
            tenant.id,
            source="salla",
            status="in_progress",
            external_id="257404293",
            external_order_number="257404293",
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"product_title": GENERIC_SHIRT, "quantity": 1}],
            extra_metadata={"created_at": "2026-05-04T10:00:00+00:00"},
        )
        open_order.customer_id = customer.id
        db.commit()
        facts = _facts()
        titles = catalog_reasoning_titles(facts=facts)
        assert GENERIC_SHIRT in titles
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=tenant.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer.id,
            conversation_id=conv.id,
        )
        assert payload is not None
        assert payload["current_order"]["display_reference"] == "257404293"
        visual = collect_visual_delivery_capability(facts=facts)
        assert visual_delivery_available(visual) is True
        browse = DefaultDecisionEngine().decide(
            _ctx("وريني شي مناسب", intent_name=INTENT_ASK_PRODUCT, facts=facts),
        )
        assert browse.action != ACTION_TRACK_ORDER
        current = DefaultDecisionEngine().decide(
            _ctx("وين طلبي الحالي؟", intent_name=INTENT_TRACK_ORDER, facts=facts),
        )
        assert current.action == ACTION_TRACK_ORDER
        back_to_shop = DefaultDecisionEngine().decide(
            _ctx("طيب نرجع للتسوق وريني شي مناسب", intent_name=INTENT_ASK_PRODUCT, facts=facts),
        )
        assert back_to_shop.action != ACTION_TRACK_ORDER
        from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler

        switch_ctx = _ctx(
            "طيب نرجع للتسوق",
            intent_name=INTENT_ASK_PRODUCT,
            facts=facts,
        )
        switch_ctx.state.conversation_focus = "order_tracking"
        rows = CatalogNavigateHandler()._fallback_top_products(switch_ctx, limit=8)
        switch_titles = {str(r.get("title") or "") for r in rows}
        assert GENERIC_SHIRT in switch_titles
        media = DefaultDecisionEngine().decide(
            _ctx(
                "نعم ارسل صور",
                intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
                facts=facts,
                state=MerchantConversationState(
                    stage="exploring",
                    turn=8,
                    greeted=True,
                    last_presented_products=list(facts.discovery_products),
                ),
            ),
        )
        assert media.action == ACTION_SEARCH_PRODUCTS
        pay = classify_fact_answer("وش طرق الدفع")
        loc = classify_fact_answer("وفرعكم وين؟")
        assert pay is not None and loc is not None
        assert pay.fact_kind == KIND_PAYMENT_METHODS
        assert loc.fact_kind in {KIND_LOCATION, KIND_BRANCH_EXISTENCE}


class TestMultiTenantIsolation:
    def test_foreign_tenant_orders_are_invisible(self, db) -> None:
        a = seed_tenant(db, name="متجر أ")
        b = seed_tenant(db, name="متجر ب")
        customer_a = seed_customer(db, a.id, name=GENERIC_CUSTOMER)
        customer_b = seed_customer(db, b.id, phone="+966500000088", name="نورة عبدالله")
        conv_a = seed_conversation(db, a.id, customer_id=customer_a.id)
        order_b = seed_order(
            db,
            b.id,
            source="salla",
            status="in_progress",
            external_id="999111000",
            external_order_number="999111000",
            customer_info={"phone": "+966500000088"},
            line_items=[{"product_title": GENERIC_PERFUME, "quantity": 1}],
        )
        order_b.customer_id = customer_b.id
        db.commit()
        payload = collect_customer_order_evidence(
            db=db,
            tenant_id=a.id,
            phone=DEFAULT_PHONE_E164,
            customer_id=customer_a.id,
            conversation_id=conv_a.id,
        )
        assert payload is not None
        refs = {row["display_reference"] for row in payload["orders"]}
        assert "999111000" not in refs
        names = {
            item["name"]
            for row in payload["orders"]
            for item in row.get("line_items") or []
        }
        assert GENERIC_PERFUME not in names


class TestProductPronounContinuity:
    def test_deictic_visual_does_not_treat_discourse_as_sku(self) -> None:
        from modules.ai.brain.commerce.product_visual import (
            extract_visual_product_query,
            is_deictic_visual_request,
        )

        msg = "وريني صورته"
        assert extract_visual_product_query(msg) == ""
        assert is_deictic_visual_request(msg) is True
        state = MerchantConversationState(stage="exploring", turn=6, greeted=True)
        state.last_presented_products = list(_facts().discovery_products)
        state.last_recommended_products = [
            {
                "id": 12,
                "title": GENERIC_SHIRT,
                "external_id": "ext-shirt",
                "image_url": "https://cdn.example.test/shirt.jpg",
                "provenance": "assistant_recommended",
                "customer_selected": False,
            }
        ]
        ctx = _ctx(
            msg,
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
        )
        ctx.intent.slots["product_query"] = "وريني"
        decision = DefaultDecisionEngine().decide(ctx)
        query = str((decision.args or {}).get("query") or "")
        assert query != "وريني"
        assert "وريني" not in query
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("after_search") == "product_visual"
        assert decision.args.get("force_product_card") is True
        replay = list(decision.args.get("replay_candidates") or [])
        assert replay
        assert str(replay[0].get("title") or "") == GENERIC_SHIRT

    def test_assistant_presented_catalog_survives_for_pronoun(self) -> None:
        from modules.ai.brain.commerce.product_visual import resolve_trusted_focus_for_deictic

        state = MerchantConversationState(stage="exploring", turn=6, greeted=True)
        state.last_presented_products = [
            {"title": GENERIC_SHIRT, "id": 12, "provenance": "assistant_presented"},
        ]
        trusted = resolve_trusted_focus_for_deictic(state, "وريني صورته")
        assert trusted.title == GENERIC_SHIRT
        assert trusted.product_id == "12"
        assert trusted.origin == "last_presented_products"
        assert state.selected_product_id == ""


class TestTopicSwitchCatalogFallback:
    def test_top_fallback_uses_discovery_when_ranker_empty(self) -> None:
        from modules.ai.brain.execution.catalog_navigate import CatalogNavigateHandler

        handler = CatalogNavigateHandler()
        ctx = _ctx("طيب نرجع للتسوق", intent_name=INTENT_ASK_PRODUCT)
        ctx.state.conversation_focus = "order_tracking"
        rows = handler._fallback_top_products(ctx, limit=8)
        titles = {str(r.get("title") or "") for r in rows}
        assert GENERIC_SHIRT in titles
        assert GENERIC_SHOE in titles


class TestAssistantPresentedProvenance:
    def test_named_list_stamps_presented_not_customer_selected(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=4, greeted=True)
        candidates = list(_facts().discovery_products)
        reply = (
            "1. **حذاء رياضي أبيض** بسعر 120 ريال.\n"
            "2. **قميص قطني أزرق** بسعر 80 ريال."
        )
        stamped = stamp_assistant_named_catalog_from_reply(
            state=state,
            reply=reply,
            catalog_candidates=candidates,
            turn=4,
        )
        titles = {str(row.get("title") or "") for row in stamped}
        assert GENERIC_SHOE in titles
        assert GENERIC_SHIRT in titles
        assert state.last_recommended_products == []
        assert all(row.get("customer_selected") is False for row in state.last_presented_products)
        assert state.current_product_focus in (None, {})
        assert state.selected_product_id == ""

    def test_unique_named_recommendation_stamps_recommended_id(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=5, greeted=True)
        candidates = list(_facts().discovery_products)
        stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="1. حذاء رياضي أبيض\n2. قميص قطني أزرق",
            catalog_candidates=candidates,
            turn=4,
        )
        stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="أنصحك بالقميص قطني أزرق",
            catalog_candidates=candidates,
            turn=5,
        )
        recommended = list(state.last_recommended_products or [])
        assert len(recommended) == 1
        assert recommended[0]["id"] == 12
        assert recommended[0]["provenance"] == "assistant_recommended"
        assert recommended[0].get("customer_selected") is False
        assert state.current_product_focus in (None, {})

    def test_price_disambiguates_same_title_family(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=3, greeted=True)
        candidates = [
            {"id": 22, "title": "فستان", "external_id": "dress-149", "price": 149.0},
            {"id": 23, "title": "فستان", "external_id": "dress-289", "price": 289.0},
            {"id": 28, "title": "جاكيت", "external_id": "jacket-169", "price": 169.0},
        ]
        stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="1. **فستان** بسعر 289 ريال.\n2. **جاكيت** بسعر 169 ريال.",
            catalog_candidates=candidates,
            turn=3,
        )
        ids = {row.get("id") for row in state.last_presented_products}
        assert 23 in ids
        assert 22 not in ids
        stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="أنصحك بالفستان",
            catalog_candidates=candidates,
            turn=4,
        )
        assert state.last_recommended_products[0]["id"] == 23


class TestPresentedProductVisualFamilies:
    def test_recommended_product_visual_executes(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=6, greeted=True)
        state.last_presented_products = list(_facts().discovery_products)
        state.last_recommended_products = [
            {
                "id": 12,
                "title": GENERIC_SHIRT,
                "external_id": "ext-shirt",
                "image_url": "https://cdn.example.test/shirt.jpg",
                "provenance": "assistant_recommended",
                "customer_selected": False,
            }
        ]
        ctx = _ctx(
            "وريني صورته",
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("after_search") == "product_visual"
        replay = list(decision.args.get("replay_candidates") or [])
        assert replay[0]["id"] == 12
        assert replay[0]["title"] == GENERIC_SHIRT

    def test_ambiguous_presented_products_do_not_canned_clarify(self) -> None:
        state = MerchantConversationState(stage="exploring", turn=6, greeted=True)
        state.last_presented_products = list(_facts().discovery_products)
        ctx = _ctx(
            "وريني صورته",
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CLARIFY
        assert decision.args.get("response_goal") == "resolve_visual_referent"

    def test_customer_selected_product_visual_wins(self) -> None:
        from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus

        state = MerchantConversationState(stage="exploring", turn=7, greeted=True)
        state.last_presented_products = list(_facts().discovery_products)
        set_product_focus(
            state,
            {
                "id": 11,
                "title": GENERIC_SHOE,
                "external_id": "ext-shoe",
                "image_url": "https://cdn.example.test/shoe.jpg",
            },
            reason="customer_pick",
            turn=7,
        )
        ctx = _ctx(
            "وريني صورته",
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        replay = list(decision.args.get("replay_candidates") or [])
        assert replay[0]["id"] == 11
        assert replay[0]["title"] == GENERIC_SHOE


class TestOrderToShoppingContinuation:
    def test_populated_catalog_does_not_replace_llm_shopping_reply(self) -> None:
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"
        llm = "أكيد! عندك خيارات رائعة إذا تبغى هدايا أو أي شيء آخر. وش تحب تشتري اليوم؟"
        result = apply_catalog_product_grounding_guard(
            reply=llm,
            inbound_text="طيب نرجع للتسوق",
            inbound_metadata={
                "intent": INTENT_ASK_PRODUCT,
                "catalog_reasoning_titles": [GENERIC_SHIRT, GENERIC_SHOE, "فستان"],
            },
            intent=_intent("طيب نرجع للتسوق", INTENT_ASK_PRODUCT),
        )
        assert result.replaced is False
        assert result.reply == llm
        assert "الكتالوج" not in result.reply

    def test_grounded_recommendation_is_not_rewritten(self) -> None:
        os.environ["NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE"] = "enforce"
        llm = "أنصحك بالفستان، لأنه مثالي كهدية ويعطي لمسة أنيقة. تبي أرسل لك تفاصيله أو صورة؟"
        result = apply_catalog_product_grounding_guard(
            reply=llm,
            inbound_text="اختاري لي أنت",
            inbound_metadata={"catalog_reasoning_titles": ["فستان", "جاكيت", "تنورة"]},
        )
        assert result.replaced is False
        assert "فستان" in result.reply
        assert "الكتالوج" not in result.reply


class TestRepeatedTopicSwitchOwnership:
    def test_order_payment_catalog_do_not_leave_stale_order_owner(self) -> None:
        from modules.ai.brain.commerce.commerce_focus_owner import (
            FOCUS_ORDER_TRACKING,
            apply_commerce_focus_lifecycle,
        )

        state = MerchantConversationState(stage="exploring", turn=10, greeted=True)
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_ASK_PRODUCT,
            action="search_products",
            message="وش عندكم؟",
            turn=10,
        )
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_TRACK_ORDER,
            action="track_order",
            message="وين طلبي الحالي؟",
            turn=11,
        )
        assert state.conversation_focus == FOCUS_ORDER_TRACKING
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_ASK_PRODUCT,
            action="llm_reply",
            message="طيب نرجع للتسوق",
            turn=12,
        )
        assert state.conversation_focus != FOCUS_ORDER_TRACKING
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_ASK_PAYMENT_INFO,
            action="faq_reply",
            message="وش طرق الدفع",
            turn=13,
        )
        apply_commerce_focus_lifecycle(
            state,
            intent_name=INTENT_ASK_PRODUCT,
            action="llm_reply",
            message="وريني شي مناسب",
            turn=14,
        )
        assert state.conversation_focus != FOCUS_ORDER_TRACKING

