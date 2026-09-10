"""Regression: Salla discovery browse → category pick → direct product presentation."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    apply_turn_catalog_referent_binding,
    stamp_assistant_named_catalog_from_reply,
    structured_product_from_turn,
)
from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_DISCOVERY,
)
from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_SINGLE_RICH,
    apply_search_product_presentation,
    presentation_context_from_brain,
)
from modules.ai.brain.commerce.category_browse_selection_pick import (  # noqa: E402
    try_named_product_link_decision,
)
from modules.ai.brain.commerce.selection_context import (  # noqa: E402
    stamp_selection_context_from_products,
    try_category_browse_pick_decision,
    try_selection_context_decision,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    _catalog_product_answer_emergency_fallback,
    build_catalog_product_answer_facts_bundle,
    try_compose_catalog_product_answer,
)
from modules.ai.brain.persona.fact_bound_composer import FactBoundPersonaComposer  # noqa: E402
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402
from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: E402
    build_availability_context,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from modules.ai.postprocess.safety_nets import apply_store_link_safety_net  # noqa: E402
from routers.whatsapp_webhook import build_cta_url_payload  # noqa: E402

JACKET_IMAGE_URL = (
    "https://salla-dev.s3.eu-central-1.amazonaws.com/nWzD/"
    "iBBzFqENAn2B2X6cy8ssWkuzS7PAhZ9OQqpnd5ov.jpg"
)
JACKET_PRODUCT_URL = (
    "https://demostore.salla.sa/dev-cgcaqkpx5wgewsyv/جاكيت/p1921568272"
)

DRESS_22 = {
    "id": 22,
    "external_id": "1638893598",
    "title": "فستان",
    "display_label": "فستان",
    "price": 149.0,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "customer_selected": False,
    "provenance": "assistant_presented",
}
DRESS_23 = {
    "id": 23,
    "external_id": "398551325",
    "title": "فستان",
    "display_label": "فستان",
    "price": 289.0,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "customer_selected": False,
    "provenance": "assistant_presented",
}
JACKET_28 = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "display_label": "جاكيت",
    "price": 169.0,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": JACKET_IMAGE_URL,
    "product_url": JACKET_PRODUCT_URL,
    "needs_variant_choice": True,
    "has_variants": True,
    "default_variant_id": 39,
    "customer_selected": False,
    "provenance": "assistant_presented",
}
PRESENTED_LIVE = [DRESS_22, DRESS_23, JACKET_28]

GENERIC_JACKETS = [
    {
        "id": 701,
        "external_id": "sku-jacket-sport",
        "title": "جاكيت رياضي",
        "display_label": "جاكيت رياضي",
        "price": 199.0,
        "in_stock": True,
        "can_checkout": True,
        "image_url": "https://cdn.example/jacket-sport.jpg",
        "product_url": "https://shop.example/products/jacket-sport",
    },
]
GENERIC_SHOE = {
    "id": 801,
    "external_id": "sku-white-running-shoe",
    "title": "حذاء رياضي أبيض",
    "display_label": "حذاء رياضي أبيض",
    "price": 220.0,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/white-running-shoe.jpg",
    "product_url": "https://shop.example/products/white-running-shoe",
}


def _facts(products: List[Dict[str, Any]]) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=len(products),
        in_stock_count=len(products),
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        top_products=list(products),
        discovery_products=list(products),
    )


def _presented_state(
    products: List[Dict[str, Any]],
    *,
    turn: int = 3,
) -> MerchantConversationState:
    state = MerchantConversationState(
        greeted=True,
        stage="exploring",
        turn=turn,
        commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        last_browse_query="وش منتجاتكم؟",
        last_presentation_mode="discovery_list",
    )
    stamp_selection_context_from_products(state, products=products)
    state.last_search_candidates = list(products)
    return state


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    products: List[Dict[str, Any]] | None = None,
    tenant_id: int = 1,
) -> BrainContext:
    catalog = products if products is not None else PRESENTED_LIVE
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state or _presented_state(catalog),
        facts=_facts(catalog),
    )


def _run_search(decision, ctx) -> Any:
    return asyncio.run(ProductSearchHandler().handle(decision, ctx))


def _apply_presentation(decision, ctx, result_data: Dict[str, Any]):
    resolved = result_data.get("product")
    return apply_search_product_presentation(
        result_data,
        candidates=(
            [resolved]
            if isinstance(resolved, dict)
            else list(result_data.get("products") or [])
        ),
        **presentation_context_from_brain(
            ctx,
            decision,
            resolved_product=resolved if isinstance(resolved, dict) else None,
        ),
    )


def _presentation_flow(message: str, *, state=None, products=None):
    ctx = _ctx(message, state=state, products=products)
    decision = try_selection_context_decision(ctx) or try_category_browse_pick_decision(ctx)
    assert decision is not None
    result = _run_search(decision, ctx)
    presentation = _apply_presentation(decision, ctx, result.data)
    return ctx, decision, result, presentation


def _guard_catalog_browse_fallback(
    *,
    text: str,
    products: List[Dict[str, Any]],
    pending_product_card_count: int,
):
    prev_mode = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
    os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
    result_data = {
        "question_kind": "browse",
        "compose_source": "fallback_deterministic",
        "fallback_action_type": "catalog_product_answer",
        "eligible_product_count": len(products),
        "catalog_search_query": "",
        "search_result_count": len(products),
        "pending_product_card_count": pending_product_card_count,
        "pending_candidates": list(products),
    }
    availability_context = build_availability_context(
        None,
        1,
        result_data=result_data,
    )
    # The fixture is the known false catalog-denial fallback. Claim extraction
    # is an explicit model-boundary fixture, not a language-understanding test.
    import json
    from modules.ai.brain.postprocess.catalog_semantic_claims import parse_claims

    semantics = parse_claims(json.dumps({"complete": True, "confidence": 1, "claims": [{
        "scope": "catalog", "product_id": None, "attribute": "exists", "value": False,
        "quote": text,
    }]}), text, availability_context["catalog_presentation_facts"])
    guarded = apply_product_availability_truth_guard(
        reply=text,
        availability_context=availability_context,
        inbound_text="وش المنتجات المتوفرة؟",
        chosen_path="fact_bound_persona_compose",
        question_kind="browse",
        surface="catalog_product_answer",
        allow_recompose=True,
        semantic_claims=semantics,
    )
    second_reply = guarded.reply
    if guarded.requires_grounded_recompose:
        second_reply = guarded.reply
    passed = apply_product_availability_truth_guard(
        reply=second_reply,
        availability_context=availability_context,
        inbound_text="وش المنتجات المتوفرة؟",
        chosen_path="fact_bound_persona_compose",
        question_kind="browse",
        surface="catalog_product_answer",
        allow_recompose=False,
        semantic_claims=semantics,
    )
    if prev_mode is None:
        os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
    else:
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev_mode
    return guarded, passed, availability_context


class TestSallaCategoryPickPresentation:
    def test_browse_reply_stamps_category_scoped_products(self) -> None:
        state = MerchantConversationState(turn=1)
        stamped = stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="متوفر عندنا فساتين وجاكيتات",
            catalog_candidates=PRESENTED_LIVE,
            turn=1,
        )
        assert len(stamped) >= 2
        ids = {int(row.get("id") or 0) for row in stamped}
        assert 28 in ids
        jacket = next(row for row in stamped if int(row.get("id") or 0) == 28)
        assert jacket.get("product_url") == JACKET_PRODUCT_URL
        assert jacket.get("image_url") == JACKET_IMAGE_URL

    def test_category_query_sends_matching_products(self) -> None:
        _, decision, result, _ = _presentation_flow("ابي الجاكيتات")
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") in {
            "selection_context_category_present",
            "selection_context_category_browse_pick",
        }
        products = result.data.get("products") or decision.args.get("products") or []
        assert len(products) == 1
        assert int(products[0]["id"]) == 28

    def test_exact_product_query_sends_image_and_direct_url(self) -> None:
        _, _, result, presentation = _presentation_flow("ابي الجاكيتات")
        assert presentation.kind == PRESENTATION_SINGLE_RICH
        card = (result.data.get("pending_product_cards") or [None])[0]
        assert card is not None
        assert card["file_url"] == JACKET_IMAGE_URL
        assert card["product_url"] == JACKET_PRODUCT_URL
        payload = build_cta_url_payload(
            to="966500000001",
            body_text=str(card.get("caption") or card.get("title") or ""),
            btn_label="عرض المنتج",
            btn_url=str(card["product_url"]),
            header_image_url=str(card["file_url"]),
        )
        assert payload is not None
        assert payload["interactive"]["action"]["parameters"]["url"] == JACKET_PRODUCT_URL

    def test_named_product_link_uses_presented_identity(self) -> None:
        _, decision, result, presentation = _presentation_flow("ابي رابط الجاكيت")
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "selection_context_named_product_link"
        card = (result.data.get("pending_product_cards") or [None])[0]
        assert card is not None
        assert card["product_url"] == JACKET_PRODUCT_URL
        assert presentation.kind == PRESENTATION_SINGLE_RICH

    def test_three_turn_acceptance_sequence(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="exploring",
            turn=1,
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        stamp_assistant_named_catalog_from_reply(
            state=state,
            reply="متوفر عندنا فساتين وجاكيتات",
            catalog_candidates=PRESENTED_LIVE,
            turn=1,
        )
        state.turn = 2
        _, decision_2, result_2, presentation_2 = _presentation_flow(
            "ابي الجاكيتات",
            state=state,
            products=PRESENTED_LIVE,
        )
        assert decision_2.action == ACTION_SEARCH_PRODUCTS
        assert len(result_2.data.get("products") or []) == 1
        assert presentation_2.kind == PRESENTATION_SINGLE_RICH

        state.turn = 3
        _, decision_3, result_3, presentation_3 = _presentation_flow(
            "ابي رابط الجاكيت",
            state=state,
            products=PRESENTED_LIVE,
        )
        assert decision_3.args.get("source") == "selection_context_named_product_link"
        card = (result_3.data.get("pending_product_cards") or [None])[0]
        assert card["product_url"] == JACKET_PRODUCT_URL
        assert presentation_3.kind == PRESENTATION_SINGLE_RICH

    def test_named_product_link_falls_back_to_prior_inbound_focus(self) -> None:
        """Live gap: rich card focus without jacket in last_presented_products."""
        state = MerchantConversationState(
            greeted=True,
            stage="exploring",
            turn=2,
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            last_presented_products=[DRESS_22, DRESS_23],
        )
        set_product_focus(
            state,
            dict(JACKET_28),
            reason="executor_product_search_products",
            turn=2,
        )
        decision = try_named_product_link_decision(_ctx("ابي رابط الجاكيت", state=state))
        assert decision is not None
        assert decision.args.get("source") == "selection_context_named_product_link"
        assert decision.args.get("presentation_identity_grounded") is True
        product = (decision.args.get("products") or [None])[0]
        assert product is not None
        assert product.get("product_url") == JACKET_PRODUCT_URL

    def test_live_replay_pdp_persists_from_rich_card_to_named_link(self) -> None:
        """Replay the observed three turns through routing, delivery, and fallback."""
        live_messages = (
            "وش منتجاتكم؟",
            "ابي الجاكيتات",
            "ابي رابط الجاكيت",
        )
        state = MerchantConversationState(
            greeted=True,
            stage="exploring",
            turn=1,
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        assert _ctx(live_messages[0], state=state).message == live_messages[0]
        stamp_selection_context_from_products(
            state,
            products=[DRESS_22, DRESS_23],
        )

        state.turn = 2
        _, decision_2, result_2, presentation_2 = _presentation_flow(
            live_messages[1],
            state=state,
            products=PRESENTED_LIVE,
        )
        assert presentation_2.kind == PRESENTATION_SINGLE_RICH
        cards_2 = result_2.data.get("pending_product_cards") or []
        assert len(cards_2) == 1
        assert int(cards_2[0].get("id") or 0) == 28
        assert cards_2[0].get("file_url") == JACKET_IMAGE_URL
        assert decision_2.args.get("question_kind") is None

        fallback_bundle = build_catalog_product_answer_facts_bundle(
            inbound_text=live_messages[1],
            tenant_id=1,
            customer_phone="966500000001",
            products=list(result_2.data.get("pending_candidates") or []),
            catalog_search_query="جاكيت",
            question_kind=str(decision_2.args.get("question_kind") or ""),
            display_count=1,
            decision_args=dict(decision_2.args or {}),
        )
        fallback_facts = fallback_bundle.verified_facts
        assert fallback_facts["eligible_product_count"] == 1
        assert fallback_facts["catalog_products"][0]["available"] is True
        assert fallback_facts["catalog_products"][0]["orderable"] is True
        fallback_2 = _catalog_product_answer_emergency_fallback(
            fallback_bundle,
            reason="invented_offer",
        )
        assert fallback_facts["question_kind"] == "browse"
        guarded_2, passed_2, _ = _guard_catalog_browse_fallback(
            text=fallback_2.text,
            products=list(result_2.data.get("pending_candidates") or []),
            pending_product_card_count=len(cards_2),
        )
        assert guarded_2.requires_grounded_recompose is True
        assert guarded_2.replaced is False
        assert guarded_2.action == "rewrite_false_negative"
        assert passed_2.replaced is True
        assert "متوفر" not in passed_2.reply
        assert passed_2.action == "rewrite_false_negative"

        structured = structured_product_from_turn(decision_2, result_2)
        assert structured is not None
        assert int(structured.get("id") or 0) == 28
        apply_turn_catalog_referent_binding(
            state=state,
            reply="",
            structured_product=structured,
            turn=2,
        )
        jacket_presented = any(
            int(row.get("id") or 0) == 28
            for row in (state.last_presented_products or [])
            if isinstance(row, dict)
        )
        assert jacket_presented
        assert state.current_product_focus.get("product_url") == JACKET_PRODUCT_URL

        state.turn = 2
        ctx_3 = _ctx(
            live_messages[2],
            state=state,
            products=PRESENTED_LIVE,
        )
        decision_3 = DefaultDecisionEngine().decide(ctx_3)
        assert decision_3.args.get("source") == "selection_context_named_product_link"
        assert decision_3.args.get("presentation_identity_grounded") is True
        result_3 = _run_search(decision_3, ctx_3)
        presentation_3 = _apply_presentation(decision_3, ctx_3, result_3.data)
        assert presentation_3.kind == PRESENTATION_SINGLE_RICH
        assert result_3.data.get("presentation_identity_grounded") is True
        cards_3 = result_3.data.get("pending_product_cards") or []
        assert len(cards_3) == 1
        card = cards_3[0]
        assert card is not None
        assert int(card.get("id") or 0) == 28
        assert card["product_url"] == JACKET_PRODUCT_URL
        assert card["file_url"] == JACKET_IMAGE_URL
        assert card["product_url"].rstrip("/") != "https://demostore.salla.sa"

        provider_payload = build_cta_url_payload(
            to="966500000001",
            body_text=str(card.get("caption") or card.get("title") or ""),
            btn_label="عرض المنتج",
            btn_url=str(card["product_url"]),
            header_image_url=str(card["file_url"]),
        )
        assert provider_payload is not None
        assert (
            provider_payload["interactive"]["action"]["parameters"]["url"]
            == JACKET_PRODUCT_URL
        )

        store_link_net = apply_store_link_safety_net(
            db=None,
            tenant_id=1,
            customer_msg=live_messages[2],
            reply_text=str(card.get("title") or ""),
        )
        assert store_link_net.fired is False
        assert store_link_net.rewrote_reply is False
        assert store_link_net.new_reply == ""

    @pytest.mark.parametrize(
        "message",
        (
            "ابي رابط حذاء رياضي أبيض",
            "ارسل لينك حذاء رياضي أبيض",
            "ابعثلي رابط حذاء رياضي أبيض",
        ),
    )
    def test_named_product_link_variants_precede_ce2(self, message: str) -> None:
        state = _presented_state([GENERIC_SHOE], turn=4)
        ctx = _ctx(message, state=state, products=[GENERIC_SHOE], tenant_id=2)

        decision = DefaultDecisionEngine().decide(ctx)

        assert decision.args.get("source") == "selection_context_named_product_link"
        assert decision.args.get("presentation_identity_grounded") is True
        product = (decision.args.get("products") or [None])[0]
        assert product is not None
        assert product.get("product_url") == GENERIC_SHOE["product_url"]

    def test_named_product_link_rejects_mismatched_or_stale_focus(self) -> None:
        mismatched = MerchantConversationState(turn=5)
        set_product_focus(
            mismatched,
            dict(GENERIC_SHOE),
            reason="executor_product_search_products",
            turn=5,
        )
        assert try_named_product_link_decision(
            _ctx("ابي رابط الجاكيت", state=mismatched)
        ) is None

        stale = MerchantConversationState(turn=30)
        set_product_focus(
            stale,
            dict(GENERIC_SHOE),
            reason="executor_product_search_products",
            turn=1,
        )
        assert try_named_product_link_decision(
            _ctx("ابي رابط حذاء رياضي أبيض", state=stale, products=[GENERIC_SHOE])
        ) is None

        without_pdp = dict(GENERIC_SHOE)
        without_pdp.pop("product_url")
        no_url = _presented_state([without_pdp], turn=5)
        assert try_named_product_link_decision(
            _ctx("ابي رابط حذاء رياضي أبيض", state=no_url, products=[without_pdp])
        ) is None

    def test_browse_one_eligible_cannot_negate_after_invented_offer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def rejected_compose(
            _composer: FactBoundPersonaComposer,
            bundle: Any,
        ) -> PersonaComposeResult:
            return PersonaComposeResult(
                text="",
                source="fallback_deterministic",
                surface=bundle.surface,
                facts_hash="attempted",
                guard_passed=False,
                fallback_reason="invented_offer",
                language=bundle.language,
                dialect=bundle.dialect,
            )

        monkeypatch.setattr(FactBoundPersonaComposer, "compose", rejected_compose)
        text, result, event = asyncio.run(
            try_compose_catalog_product_answer(
                tenant_id=2,
                customer_phone="966500000002",
                inbound_text="ابي الأحذية",
                products=[dict(GENERIC_SHOE)],
                catalog_search_query="حذاء رياضي أبيض",
                question_kind="browse",
                display_count=1,
            )
        )

        assert result.source == "fallback_deterministic"
        assert result.fallback_reason == "invented_offer"
        assert event["eligible_product_count"] == 1
        assert event["question_kind"] == "browse"
        assert event["catalog_product_ids"] == [GENERIC_SHOE["id"]]
        guarded, passed, availability_context = _guard_catalog_browse_fallback(
            text=text,
            products=[dict(GENERIC_SHOE)],
            pending_product_card_count=1,
        )
        presentation_facts = availability_context["catalog_presentation_facts"]
        assert presentation_facts["eligible_product_count"] == 1
        assert presentation_facts["has_eligible_products"] is True
        assert presentation_facts["pending_product_card_count"] == 1
        assert guarded.requires_grounded_recompose is True
        assert guarded.replaced is False
        assert guarded.action == "rewrite_false_negative"
        assert guarded.reply == text
        assert passed.replaced is True
        assert "متوفر" not in passed.reply
        assert passed.action == "rewrite_false_negative"

    def test_browse_multiple_eligible_cannot_negate(self) -> None:
        products = [dict(GENERIC_SHOE), dict(JACKET_28)]
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="وش المنتجات المتوفرة؟",
            tenant_id=2,
            customer_phone="966500000002",
            products=products,
            question_kind="browse",
            display_count=2,
        )
        fallback = _catalog_product_answer_emergency_fallback(
            bundle,
            reason="invented_offer",
        )

        assert bundle.verified_facts["question_kind"] == "browse"
        assert bundle.verified_facts["eligible_product_count"] == 2
        guarded, passed, availability_context = _guard_catalog_browse_fallback(
            text=fallback.text,
            products=products,
            pending_product_card_count=0,
        )
        presentation_facts = availability_context["catalog_presentation_facts"]
        assert presentation_facts["eligible_product_count"] == 2
        assert presentation_facts["has_eligible_products"] is True
        assert presentation_facts["pending_product_card_count"] == 0
        assert guarded.requires_grounded_recompose is True
        assert guarded.replaced is False
        assert guarded.action == "rewrite_false_negative"
        assert guarded.reply == fallback.text
        assert passed.replaced is True
        assert "متوفر" not in passed.reply
        assert passed.action == "rewrite_false_negative"

    def test_browse_emergency_without_eligible_may_deny_catalog(self) -> None:
        bundle = build_catalog_product_answer_facts_bundle(
            inbound_text="وش المنتجات المتوفرة؟",
            tenant_id=2,
            customer_phone="966500000002",
            products=[],
            question_kind="browse",
            display_count=0,
        )
        fallback = _catalog_product_answer_emergency_fallback(
            bundle,
            reason="invented_offer",
        )
        assert bundle.verified_facts["question_kind"] == "browse"
        assert int(bundle.verified_facts.get("eligible_product_count") or 0) == 0
        assert "لا توجد منتجات قابلة للبيع" in fallback.text

    def test_generic_merchant_category_pick_without_prior_stamp(self) -> None:
        ctx = _ctx(
            "ابي الجاكيتات",
            state=MerchantConversationState(turn=1),
            products=GENERIC_JACKETS,
            tenant_id=2,
        )
        decision = try_category_browse_pick_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        products = decision.args.get("products") or []
        assert len(products) == 1
        assert int(products[0]["id"]) == 701
