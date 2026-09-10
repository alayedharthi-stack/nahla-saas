"""Product understanding: AI chooses; code tools/execute/verify.

Deterministic contracts for catalog tools, focus, presentation, and the
availability guard. Isolated live-model eval is skipped unless OPENAI_API_KEY
is present — a mock compose path is not proof of model understanding.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.store_knowledge import CatalogSearchProductsResult  # noqa: E402
from modules.ai.brain.commerce.catalog_search_evidence import (  # noqa: E402
    has_catalog_search_evidence,
)
from modules.ai.brain.commerce.category_browse_selection_pick import (  # noqa: E402
    try_named_product_link_decision,
)
from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_DISCOVERY,
)
from modules.ai.brain.commerce.product_option_capture import (  # noqa: E402
    capture_pending_option_value,
)
from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_MULTI_CHOICES,
    PRESENTATION_SINGLE_RICH,
    apply_search_product_presentation,
    presentation_context_from_brain,
    resolve_product_presentation,
)
from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    is_deictic_visual_request,
    is_product_visual_request,
)
from modules.ai.brain.commerce.selection_context import (  # noqa: E402
    stamp_selection_context_from_products,
    try_category_browse_pick_decision,
    try_selection_context_decision,
)
from modules.ai.brain.commerce.visual_delivery_capability import (  # noqa: E402
    try_visual_catalog_send_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent.social_classifier import classify_social  # noqa: E402
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
from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: E402
from routers.whatsapp_webhook import build_cta_url_payload  # noqa: E402

SHOE = {
    "id": 801,
    "external_id": "sku-white-running-shoe",
    "title": "حذاء رياضي أبيض",
    "display_label": "حذاء رياضي أبيض",
    "price": 249,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/white-running-shoe.jpg",
    "product_url": "https://shop.example/products/white-running-shoe",
    "variants_summary": "أبيض، أزرق",
}
PERFUME = {
    "id": 802,
    "external_id": "sku-rose-perfume",
    "title": "عطر ورد 100ml",
    "display_label": "عطر ورد 100ml",
    "price": 180,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/rose-perfume.jpg",
    "product_url": "https://shop.example/products/rose-perfume",
}
SHIRT = {
    "id": 803,
    "external_id": "sku-blue-cotton-shirt",
    "title": "قميص قطني أزرق",
    "display_label": "قميص قطني أزرق",
    "price": 95,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
    "image_url": "https://cdn.example/blue-shirt.jpg",
    "product_url": "https://shop.example/products/blue-shirt",
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


def _state(products: List[Dict[str, Any]], *, turn: int = 2) -> MerchantConversationState:
    state = MerchantConversationState(
        greeted=True,
        stage="exploring",
        turn=turn,
        commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        last_browse_query="وش المنتجات المتوفرة؟",
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
) -> BrainContext:
    catalog = products if products is not None else [SHOE, PERFUME, SHIRT]
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=2,
        customer_phone="966500000002",
        message=msg,
        intent=intent,
        state=state or _state(catalog),
        facts=_facts(catalog),
    )


def _availability_context(products: List[Dict[str, Any]]) -> dict:
    return build_availability_context(
        None,
        2,
        result_data={
            "question_kind": "browse",
            "eligible_product_count": len(products),
            "catalog_search_query": "",
            "search_result_count": len(products),
            "pending_product_card_count": len(products),
            "pending_candidates": list(products),
        },
    )


def _guard(reply: str, products: List[Dict[str, Any]], *, allow_recompose: bool = True):
    prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
    os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
    try:
        return apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_availability_context(products),
            inbound_text="وش المنتجات المتوفرة؟",
            chosen_path="fact_bound_persona_compose",
            question_kind="browse",
            surface="catalog_product_answer",
            allow_recompose=allow_recompose,
        )
    finally:
        if prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev


class TestCatalogToolContract:
    def test_search_products_stamps_ids_and_provenance(self) -> None:
        runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
        runtime.tenant_id = 2
        runtime.catalog = MagicMock()
        runtime.catalog.search_products.return_value = CatalogSearchProductsResult(
            products=[dict(SHOE)],
            catalog_fact_products=[],
        )
        result = asyncio.run(
            CommerceToolRuntime._tool_search_products(
                runtime,
                {"query": "حذاء رياضي", "limit": 8},
            )
        )
        assert result.ok is True
        assert result.payload["source"] == "merchant_catalog"
        row = result.payload["products"][0]
        assert row["id"] == 801
        assert row["product_id"] == 801
        assert row["source"] == "merchant_catalog"
        assert row["tenant_id"] == 2
        assert row["product_url"] == SHOE["product_url"]
        assert row["image_url"] == SHOE["image_url"]

    def test_get_product_details_uses_internal_id_not_query(self) -> None:
        runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
        runtime.tenant_id = 2
        runtime.catalog = MagicMock()
        runtime.catalog.get_by_id.return_value = dict(PERFUME)
        result = asyncio.run(
            CommerceToolRuntime._tool_get_product_details(
                runtime,
                {"product_id": 802, "query": "حذاء رياضي أبيض"},
            )
        )
        assert result.ok is True
        assert result.payload["product"]["id"] == 802
        runtime.catalog.search_products.assert_not_called()

    def test_get_product_details_unknown_id_does_not_invent(self) -> None:
        runtime = CommerceToolRuntime.__new__(CommerceToolRuntime)
        runtime.tenant_id = 2
        runtime.catalog = MagicMock()
        runtime.catalog.get_by_id.return_value = None
        result = asyncio.run(
            CommerceToolRuntime._tool_get_product_details(
                runtime,
                {"product_id": 999, "query": "حذاء رياضي أبيض"},
            )
        )
        assert result.ok is False
        assert result.error == "product_not_found"
        runtime.catalog.search_products.assert_not_called()


class TestGuardClaimBinding:
    def test_conversational_photo_line_is_kept(self) -> None:
        first = _guard("متوفر حذاء رياضي أبيض تقدر تشوف صورته.", [SHOE])
        assert first.replaced is False
        assert first.reply.endswith("صورته.")

    def test_false_negative_with_alasf_is_caught(self) -> None:
        first = _guard("حذاء رياضي أبيض غير متوفر للأسف.", [SHOE])
        assert first.requires_grounded_recompose is True
        blocked = _guard(
            first.reply,
            [SHOE],
            allow_recompose=False,
        )
        assert blocked.replaced is True
        assert blocked.reply == ""

    def test_price_is_not_moved_onto_another_product(self) -> None:
        mixed = "متوفر حذاء رياضي أبيض وعطر ورد سعره 250 ريال."
        first = _guard(mixed, [SHOE, SHIRT])
        assert first.requires_grounded_recompose is True
        blocked = _guard(mixed, [SHOE, SHIRT], allow_recompose=False)
        assert blocked.reply == ""
        assert blocked.reply != "متوفر حذاء رياضي أبيض سعره 250 ريال."


class TestCategoryPickCardAndPdp:
    def test_category_then_pick_sends_image_and_pdp(self) -> None:
        jackets = [
            {
                **SHIRT,
                "id": 701,
                "title": "جاكيت رياضي",
                "display_label": "جاكيت رياضي",
                "image_url": "https://cdn.example/jacket.jpg",
                "product_url": "https://shop.example/products/jacket-sport",
            }
        ]
        ctx = _ctx("ابي الجاكيتات", products=jackets)
        decision = try_selection_context_decision(ctx) or try_category_browse_pick_decision(ctx)
        assert decision is not None
        result = asyncio.run(ProductSearchHandler().handle(decision, ctx))
        presentation = apply_search_product_presentation(
            result.data,
            candidates=list(result.data.get("products") or []),
            **presentation_context_from_brain(ctx, decision),
        )
        assert presentation.kind == PRESENTATION_SINGLE_RICH
        card = (result.data.get("pending_product_cards") or [None])[0]
        assert card is not None
        assert card["file_url"] == jackets[0]["image_url"]
        assert card["product_url"] == jackets[0]["product_url"]
        payload = build_cta_url_payload(
            to="966500000002",
            body_text=str(card.get("caption") or card.get("title") or ""),
            btn_label="عرض المنتج",
            btn_url=str(card["product_url"]),
            header_image_url=str(card["file_url"]),
        )
        assert payload is not None
        assert payload["interactive"]["action"]["parameters"]["url"] == jackets[0]["product_url"]
        assert payload["interactive"]["header"]["image"]["link"] == jackets[0]["image_url"]


class TestCurrentProductFocusActions:
    @pytest.mark.parametrize(
        "phrase",
        ("وريني صورته", "ورني صورته", "أرسل صورته", "أشوف صورته"),
    )
    def test_varied_photo_phrasing_is_deictic_visual(self, phrase: str) -> None:
        assert is_product_visual_request(phrase) is True
        assert is_deictic_visual_request(phrase) is True

    def test_photo_request_uses_focused_product_not_a_new_search(self) -> None:
        state = _state([SHOE, PERFUME], turn=3)
        set_product_focus(state, SHOE, reason="presented", turn=2)
        ctx = _ctx("وريني صورته", state=state, products=[SHOE, PERFUME])
        decision = try_visual_catalog_send_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        product = (decision.args or {}).get("product") or {}
        assert int(product.get("id") or 0) == 801
        assert product.get("image_url") == SHOE["image_url"]

    def test_named_link_uses_presented_identity(self) -> None:
        jacket = {
            "id": 701,
            "external_id": "sku-jacket-sport",
            "title": "جاكيت",
            "display_label": "جاكيت",
            "price": 169,
            "in_stock": True,
            "can_checkout": True,
            "orderable": True,
            "image_url": "https://cdn.example/jacket.jpg",
            "product_url": "https://shop.example/products/jacket-sport",
        }
        state = _state([jacket, PERFUME], turn=3)
        set_product_focus(state, jacket, reason="presented", turn=2)
        decision = try_named_product_link_decision(
            _ctx("ابي رابط الجاكيت", state=state, products=[jacket, PERFUME])
        )
        assert decision is not None
        product = (decision.args or {}).get("products") or [{}]
        assert int(product[0].get("id") or 0) == 701
        assert product[0].get("product_url") == jacket["product_url"]

    def test_deictic_link_without_unique_subject_does_not_invent_id(self) -> None:
        state = _state([SHOE, PERFUME], turn=3)
        set_product_focus(state, SHOE, reason="presented", turn=2)
        decision = try_named_product_link_decision(
            _ctx("أرسل رابطه", state=state, products=[SHOE, PERFUME])
        )
        assert decision is None


class TestColorOptionsAreProvenOnly:
    def test_color_question_is_not_a_confirmed_option_capture(self) -> None:
        groups = [
            {
                "id": 1,
                "name": "اللون",
                "values": [
                    {"id": 11, "name": "أبيض"},
                    {"id": 12, "name": "أزرق"},
                ],
            }
        ]
        asked = capture_pending_option_value(groups, "فيه أسود؟")
        assert asked.kind == "none"
        assert asked.matched is False
        white = capture_pending_option_value(groups, "أبيض")
        assert white.kind == "matched"
        assert white.match is not None
        assert white.match.value_name == "أبيض"


class TestSocialDoesNotSearch:
    @pytest.mark.parametrize(
        "phrase",
        ("تمام يعطيك العافية", "الله يعطيك العافية", "شكرا ما قصرت"),
    )
    def test_courtesy_turns_are_social_not_catalog_search(self, phrase: str) -> None:
        social = classify_social(phrase)
        assert social is not None
        ctx = _ctx(phrase, products=[SHOE])
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS
        apply_search = has_catalog_search_evidence(ctx, phrase, decision)
        if decision.action == ACTION_SOCIAL_REPLY:
            assert apply_search is False
        else:
            assert decision.action in {"llm_reply", ACTION_SOCIAL_REPLY}


class TestAmbiguousMultiProduct:
    def test_two_identified_products_stay_multi_and_do_not_collapse(self) -> None:
        decision = resolve_product_presentation([SHOE, PERFUME])
        assert decision.kind == PRESENTATION_MULTI_CHOICES
        data: Dict[str, Any] = {}
        apply_search_product_presentation(data, candidates=[SHOE, PERFUME])
        assert not data.get("pending_product_cards")


class TestIsolatedModelUnderstandingLimits:
    def test_openai_key_gate_documents_eval_blocker(self) -> None:
        from tests.salla_acceptance.layer3_provider import (  # noqa: PLC0415
            layer3_blocker_reason,
            openai_key_present,
        )

        if openai_key_present():
            assert os.environ.get("OPENAI_API_KEY", "").strip()
            return
        reason = layer3_blocker_reason()
        assert "OPENAI_API_KEY absent" in reason

    @pytest.mark.layer3_llm
    def test_live_compose_does_not_use_mock_as_understanding_proof(self) -> None:
        from tests.salla_acceptance.layer3_provider import openai_key_present  # noqa: PLC0415

        if not openai_key_present():
            pytest.skip("OPENAI_API_KEY not set — isolated model eval blocked")
        from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
            try_compose_catalog_product_answer,
        )

        text, result, event = asyncio.run(
            try_compose_catalog_product_answer(
                tenant_id=2,
                customer_phone="966500000002",
                inbound_text="وش المنتجات المتوفرة؟",
                products=[dict(SHOE)],
                catalog_search_query="حذاء رياضي أبيض",
                question_kind="browse",
                display_count=1,
            )
        )
        assert event["catalog_product_ids"] == [SHOE["id"]]
        guarded = _guard(text, [SHOE])
        if guarded.requires_grounded_recompose:
            blocked = _guard(text, [SHOE], allow_recompose=False)
            assert blocked.reply != "حذاء رياضي أبيض غير متوفر للأسف."
            assert "250" not in blocked.reply or "عطر" not in blocked.reply
        else:
            assert "غير متوفر" not in (text or "")
        assert result.source in {"persona_llm", "fallback_deterministic"}
