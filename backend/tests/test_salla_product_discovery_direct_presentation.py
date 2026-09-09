"""Regression: Salla discovery browse → category pick → direct product presentation."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

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
from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
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
        """Replay tenant-1 live sequence without manual jacket stamp shortcuts."""
        state = MerchantConversationState(
            greeted=True,
            stage="exploring",
            turn=1,
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        stamp_selection_context_from_products(
            state,
            products=[DRESS_22, DRESS_23],
        )

        state.turn = 2
        _, decision_2, result_2, presentation_2 = _presentation_flow(
            "ابي الجاكيتات",
            state=state,
            products=PRESENTED_LIVE,
        )
        assert presentation_2.kind == PRESENTATION_SINGLE_RICH
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
        _, decision_3, result_3, presentation_3 = _presentation_flow(
            "ابي رابط الجاكيت",
            state=state,
            products=PRESENTED_LIVE,
        )
        assert decision_3.args.get("source") == "selection_context_named_product_link"
        assert presentation_3.kind == PRESENTATION_SINGLE_RICH
        card = (result_3.data.get("pending_product_cards") or [None])[0]
        assert card is not None
        assert card["product_url"] == JACKET_PRODUCT_URL
        assert card["file_url"] == JACKET_IMAGE_URL

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
