"""P0: exact unique presented identity → non-ordering rich product card.

Live contract (tenant 1): product 28 / external_id 1921568272 / title جاكيت.
Repair must use ACTION_SEARCH_PRODUCTS, not draft-order name_select.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: E402
    is_catalog_browse_message,
)
from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    _canonical_scope_token,
)
from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_DISCOVERY,
)
from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_SINGLE_RICH,
    apply_search_product_presentation,
    presentation_context_from_brain,
)
from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    is_product_visual_request,
)
from modules.ai.brain.commerce.selection_context import (  # noqa: E402
    SELECTION_CONTEXT_TTL_TURNS,
    has_active_selection_context,
    stamp_selection_context_from_products,
    try_selection_context_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
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
    "provenance": "assistant_recommended",
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
    "provenance": "assistant_recommended",
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
    "provenance": "assistant_recommended",
}

PRESENTED_LIVE = [DRESS_22, DRESS_23, JACKET_28]

SHOE_FRAGMENT_PRESENTED = [
    {
        "id": "1",
        "external_id": "sku-shoe-white",
        "title": "حذاء رياضي أبيض",
        "display_label": "حذاء رياضي أبيض",
        "price": 199,
        "can_checkout": True,
    },
    {
        "id": "2",
        "external_id": "sku-shoe-black",
        "title": "حذاء رياضي أسود",
        "display_label": "حذاء رياضي أسود",
        "price": 199,
        "can_checkout": True,
    },
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=30,
        in_stock_count=30,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        top_products=list(PRESENTED_LIVE),
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
    tenant_id: int = 1,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state or _presented_state(PRESENTED_LIVE),
        facts=_facts(),
    )


def _assert_not_checkout_selection(product: Dict[str, Any], patch: Dict[str, Any]) -> None:
    assert product.get("customer_selected") in (False, None, "")
    assert product.get("from_catalog_order") not in (True, "true")
    assert product.get("from_native_catalog_order") not in (True, "true")
    assert str(product.get("provenance") or "") != "catalog_order_selected"
    assert not str(patch.get("selected_variant_id") or "").strip()
    assert str(patch.get("selected_variant_id") or "") != "39"


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


def _identity_card_flow(message: str, *, state=None, tenant_id: int = 1):
    ctx = _ctx(message, state=state, tenant_id=tenant_id)
    decision = try_selection_context_decision(ctx)
    if decision is None:
        return ctx, None, None, None
    result = _run_search(decision, ctx)
    presentation = _apply_presentation(decision, ctx, result.data)
    return ctx, decision, result, presentation


class TestCanonicalArticleNormalization:
    def test_existing_scope_token_folds_arabic_definite_article(self) -> None:
        assert _canonical_scope_token("جاكيت") == _canonical_scope_token("الجاكيت")
        assert _canonical_scope_token("جاكيت") == "جاكيت"
        assert _canonical_scope_token("الجاكيت") == "جاكيت"


class TestExactUniquePresentedIdentity:
    def test_exact_unique_title_owns_non_ordering_search(self) -> None:
        ctx, decision, result, presentation = _identity_card_flow("جاكيت")
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == (
            "selection_context_unique_presented_identity"
        )
        products = decision.args.get("products") or []
        assert len(products) == 1
        assert int(products[0]["id"]) == 28
        assert str(products[0]["external_id"]) == "1921568272"
        assert decision.args.get("presentation_identity_grounded") is True
        _assert_not_checkout_selection(
            products[0],
            decision.args.get("selection_context_patch") or {},
        )
        assert result.success is True
        assert int((result.data.get("product") or {})["id"]) == 28
        assert presentation.kind == PRESENTATION_SINGLE_RICH
        cards = result.data.get("pending_product_cards") or []
        assert len(cards) == 1
        assert result.data.get("product_presentation_kind") == PRESENTATION_SINGLE_RICH
        assert result.data.get("presentation_identity_grounded") is True

    def test_arabic_definite_article_uses_existing_canonical_helper(self) -> None:
        _, decision, result, presentation = _identity_card_flow("الجاكيت")
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert int((decision.args.get("products") or [{}])[0]["id"]) == 28
        assert presentation.kind == PRESENTATION_SINGLE_RICH
        identity_src = inspect.getsource(
            sys.modules[
                "modules.ai.brain.commerce.selection_context"
            ]._presented_identity_key
        )
        assert "_canonical_scope_token" in identity_src
        assert 'startswith("ال")' not in identity_src
        assert 're.sub(r"^ال"' not in identity_src

    def test_duplicate_title_does_not_select_or_order(self) -> None:
        ctx, decision, result, presentation = _identity_card_flow("فستان")
        assert decision is None
        engine_decision = try_selection_context_decision(ctx)
        assert engine_decision is None
        assert presentation is None
        assert result is None

    def test_unique_fragment_path_still_resolves(self) -> None:
        state = _presented_state(SHOE_FRAGMENT_PRESENTED)
        decision = try_selection_context_decision(_ctx("رياضي أبيض", state=state))
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "selection_context_unique_fragment"
        products = decision.args.get("products") or []
        assert len(products) == 1
        assert products[0]["external_id"] == "sku-shoe-white"

    def test_explicit_purchase_name_select_still_orders(self) -> None:
        decision = try_selection_context_decision(_ctx("ابي الجاكيت"))
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("source") != (
            "selection_context_unique_presented_identity"
        )
        assert decision.args.get("presentation_identity_grounded") is not True
        assert str((decision.args.get("product") or {}).get("id") or "") == "28"

    def test_visual_request_is_not_stolen_by_identity_resolver(self) -> None:
        inbound = "ورني الجاكيت"
        decision = try_selection_context_decision(_ctx(inbound))
        assert decision is None
        assert is_product_visual_request(inbound) is True

    def test_variant_choice_required_and_default_not_selected(self) -> None:
        _, decision, result, presentation = _identity_card_flow("جاكيت")
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        product = (decision.args.get("products") or [{}])[0]
        patch = decision.args.get("selection_context_patch") or {}
        _assert_not_checkout_selection(product, patch)
        assert product.get("needs_variant_choice") is True
        assert str(patch.get("selected_variant_id") or "") != "39"
        cards = result.data.get("pending_product_cards") or []
        assert cards[0].get("needs_variant_choice") is True
        assert str(cards[0].get("default_variant_retailer_id") or "") != "39"
        assert presentation.kind == PRESENTATION_SINGLE_RICH

    def test_stale_selection_context_does_not_ground(self) -> None:
        state = _presented_state(PRESENTED_LIVE, turn=2)
        state.turn = 2 + SELECTION_CONTEXT_TTL_TURNS + 1
        assert has_active_selection_context(state) is False
        decision = try_selection_context_decision(_ctx("جاكيت", state=state))
        assert decision is None

    def test_tenant_isolation_does_not_use_other_tenant_identity(self) -> None:
        tenant_b_state = _presented_state(
            [
                {
                    "id": 99,
                    "external_id": "tenant-b-shirt",
                    "title": "قميص قطني أزرق",
                    "display_label": "قميص قطني أزرق",
                    "price": 80,
                    "can_checkout": True,
                }
            ]
        )
        decision = try_selection_context_decision(
            _ctx("جاكيت", state=tenant_b_state, tenant_id=2)
        )
        assert decision is None
        tenant_b_jacket = _presented_state(
            [
                {
                    "id": 77,
                    "external_id": "tenant-b-jacket",
                    "title": "جاكيت",
                    "display_label": "جاكيت",
                    "price": 50,
                    "can_checkout": True,
                    "image_url": "https://cdn.example/b-jacket.jpg",
                    "product_url": "https://shop-b.example/p/jacket",
                }
            ]
        )
        decision_b = try_selection_context_decision(
            _ctx("جاكيت", state=tenant_b_jacket, tenant_id=2)
        )
        assert decision_b is not None
        assert int((decision_b.args.get("products") or [{}])[0]["id"]) == 77
        assert str((decision_b.args.get("products") or [{}])[0]["external_id"]) != (
            "1921568272"
        )

    def test_title_only_row_does_not_create_trusted_card(self) -> None:
        title_only = [
            DRESS_22,
            DRESS_23,
            {
                "title": "جاكيت",
                "display_label": "جاكيت",
                "price": 169,
                "can_checkout": True,
                "image_url": JACKET_IMAGE_URL,
                "product_url": JACKET_PRODUCT_URL,
            },
        ]
        decision = try_selection_context_decision(
            _ctx("جاكيت", state=_presented_state(title_only))
        )
        assert decision is None

    def test_card_uses_canonical_synced_image_and_url(self) -> None:
        _, decision, result, presentation = _identity_card_flow("جاكيت")
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
        interactive = payload["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["action"]["parameters"]["display_text"] == "عرض المنتج"
        assert interactive["action"]["parameters"]["url"] == JACKET_PRODUCT_URL

    def test_broad_browse_is_not_stolen_after_presented_list(self) -> None:
        inbound = "وش منتجاتكم؟"
        decision = try_selection_context_decision(_ctx(inbound))
        assert decision is None
        assert is_catalog_browse_message(inbound) is True
