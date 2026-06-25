"""Commerce navigator + catalog-order continuity regressions."""
from __future__ import annotations

import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.fallback_policy import EMPTY_REPLY_OPERATIONAL_AR  # noqa: E402
from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    is_catalog_line_items_authoritative_from_prep,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    build_purchase_channel_selection_facts,
)
from modules.ai.brain.commerce.commerce_navigator import resolve_commerce_navigator  # noqa: E402
from modules.ai.brain.commerce.product_ordering_prompt import _next_missing_order_field  # noqa: E402
from modules.ai.brain.compose.templates import product_unsyncable  # noqa: E402
from modules.ai.brain.state.price_objection_topic import (  # noqa: E402
    build_price_objection_facts,
    enrich_price_objection_facts_with_active_order,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    MerchantConversationState,
    OrderPreparationState,
)

_QUANTITY_PROMPT_RE = re.compile(
    r"(?:كم\s*(?:ال)?(?:كمية|عدد)|كم\s*العدد)",
    re.UNICODE,
)
_BROWSE_SUGGESTION_RE = re.compile(
    r"(?:أكثر\s*مبيع|يمكنك\s*البحث)",
    re.UNICODE,
)
_WHICH_PRODUCT_RE = re.compile(
    r"(?:أ?ي\s*منتج\s*تقصد|توضح.*منتج)",
    re.UNICODE,
)


def _single_sku_meta(*, qty: int = 2, total: float = 195.0) -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": qty,
                "item_price": total / qty,
                "currency": "SAR",
            }
        ],
        "total_price": total,
        "total_quantity": qty,
        "line_items_count": 1,
        "currency": "SAR",
    }


def _catalog_prep_dict(*, qty: int = 2, total: float = 195.0) -> dict:
    return {
        "product_id": "100",
        "quantity": qty,
        "catalog_line_items_authoritative": True,
        "catalog_checkout_total": total,
        "catalog_checkout_currency": "SAR",
        "line_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": qty,
                "unit_price": total / qty,
                "currency": "SAR",
            }
        ],
        "missing_fields": ["city", "delivery_address"],
        "checkout_channel": "whatsapp_fast",
    }


def _catalog_prep_state(*, qty: int = 2, total: float = 195.0) -> OrderPreparationState:
    return OrderPreparationState.from_dict(_catalog_prep_dict(qty=qty, total=total))


class TestPurchaseChannelSelection:
    def test_purchase_channel_selection_offers_three_purchase_channels_without_inquiry_option(
        self,
    ) -> None:
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=True,
            store_url="https://shop.example",
        )
        facts = build_purchase_channel_selection_facts(caps)
        assert facts["available_purchase_channels"] == [
            "whatsapp_quick_order",
            "online_store",
            "showroom_visit",
        ]
        assert "inquiry" not in facts["available_purchase_channels"]

        nav = resolve_commerce_navigator(
            message="أبي أطلب",
            intent_name="start_order",
            store_url="https://shop.example",
            maps_url="https://maps.example.com/showroom",
        )
        assert nav.stage == "purchase_channel_selection"
        assert nav.available_purchase_channels == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]
        assert "inquiry" not in nav.available_purchase_channels


class TestBrowseAfterWhatsappChannel:
    def test_browse_after_whatsapp_channel_selection_does_not_fallback_compose_error(
        self,
    ) -> None:
        prep = OrderPreparationState(
            checkout_channel="whatsapp_fast",
        )
        decision = resolve_commerce_navigator(
            message="وش عندكم",
            intent_name="ask_product",
            order_prep=prep,
        )
        assert decision.stage == "browse_with_purchase_intent"
        assert decision.next_goal == "show_or_summarize_available_products_or_send_catalog"
        assert "do_not_ask_payment" in decision.forbidden_actions
        assert "do_not_ask_address_until_product_selected" in decision.forbidden_actions
        assert EMPTY_REPLY_OPERATIONAL_AR not in decision.to_dict().values()

    @pytest.mark.parametrize("message", ["وش المنتجات", "اعرض المنتجات"])
    def test_browse_variants_inside_whatsapp_checkout(self, message: str) -> None:
        prep = OrderPreparationState(checkout_channel="whatsapp_fast")
        decision = resolve_commerce_navigator(
            message=message,
            intent_name="ask_product",
            order_prep=prep,
        )
        assert decision.stage == "browse_with_purchase_intent"


class TestCatalogOrderAfterChannelSelection:
    def test_catalog_order_after_channel_selection_does_not_ask_quantity(self) -> None:
        prep = _catalog_prep_state()
        state = MerchantConversationState(order_prep=prep, cart_items=prep.line_items)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000001",
            message="[طلب كتالوج من العميل]",
            intent=None,
            facts={},
            state=state,
            profile={"inbound_metadata": _single_sku_meta()},
        )
        assert _next_missing_order_field(ctx) != "quantity"

        decision = resolve_commerce_navigator(
            message="",
            order_prep=_catalog_prep_dict(),
            inbound_metadata=_single_sku_meta(),
        )
        assert decision.stage == "whatsapp_quick_order"
        assert "quantity" not in decision.missing_fields
        assert decision.known_fields.get("quantity") == "known"

    def test_catalog_order_after_channel_selection_does_not_return_browse_suggestion(
        self,
    ) -> None:
        decision = resolve_commerce_navigator(
            message="",
            order_prep=_catalog_prep_dict(),
            inbound_metadata=_single_sku_meta(),
        )
        assert decision.stage == "whatsapp_quick_order"
        assert decision.next_goal != "help_customer_explore_products"
        assert "do_not_append_quantity_prompt" in decision.forbidden_actions

    def test_catalog_order_known_product_quantity_total_routes_to_missing_fields_only(
        self,
    ) -> None:
        decision = resolve_commerce_navigator(
            message="",
            order_prep=_catalog_prep_dict(),
            inbound_metadata=_single_sku_meta(),
        )
        assert decision.known_fields.get("product") == "known"
        assert decision.known_fields.get("quantity") == "known"
        assert decision.known_fields.get("total") == "known"
        assert "product" not in decision.missing_fields
        assert "quantity" not in decision.missing_fields
        assert set(decision.missing_fields) <= {"city", "delivery_address", "customer_first_name", "customer_last_name"}
        assert "city" in decision.missing_fields or "delivery_address" in decision.missing_fields


class TestPriceObjectionAfterCatalogOrder:
    def test_price_objection_after_catalog_order_keeps_active_order_context(self) -> None:
        msg = "سعره غالي يقول ٢٥٠"
        prep = _catalog_prep_state()
        state = MerchantConversationState(order_prep=prep, cart_items=prep.line_items)
        facts = enrich_price_objection_facts_with_active_order(
            build_price_objection_facts(msg),
            state=state,
            order_prep=_catalog_prep_dict(),
        )
        assert facts["active_catalog_order"] is True
        assert facts["current_order_total"] == pytest.approx(195.0)
        assert facts["must_not_ask_which_product_if_active_order_exists"] is True
        assert facts["must_not_offer_unapproved_discount"] is True
        assert facts.get("customer_claimed_competitor_or_expected_price") == pytest.approx(
            250.0
        )

        nav = resolve_commerce_navigator(
            message=msg,
            intent_name="ask_price",
            intent_slots={"price_objection": True},
            decision_topic="price_objection",
            order_prep=_catalog_prep_dict(),
        )
        assert nav.stage == "price_objection"
        assert nav.known_fields.get("total") == "known"
        assert "quantity" not in nav.missing_fields
