"""Catalog-authoritative line items — no free-text order rows."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.catalog_authoritative_line_items import (  # noqa: E402
    authoritative_line_items_from_prep,
    is_shipping_address_capture_context,
    line_item_has_catalog_evidence,
    order_has_authoritative_products,
)
from core.wa_cart_line_items import build_line_items_from_order_prep  # noqa: E402
from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: E402
from modules.ai.brain.commerce.commerce_navigator import resolve_commerce_navigator  # noqa: E402
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: E402


def _catalog_meta(*, qty: int = 2, total: float = 195.0) -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": "sku-123",
                "quantity": qty,
                "item_price": total / qty,
                "currency": "SAR",
            }
        ],
        "total_price": total,
        "total_quantity": qty,
        "currency": "SAR",
    }


class TestFreeTextDoesNotCreateLineItems:
    def test_free_text_product_mention_does_not_create_order_line_item(self) -> None:
        state = MerchantConversationState(stage="checkout")
        prep = OrderPreparationState(missing_fields=["city"])
        cart, deltas, changed = maybe_apply_cart_message(
            state=state,
            prep=prep,
            message="ابي ربع كيلو سمر",
            product_info=None,
        )
        assert changed is False
        assert cart == []
        assert prep.line_items == []
        assert prep.product_mentions
        assert prep.product_mentions[0]["catalog_match_status"] == "needs_review"
        assert prep.product_mentions[0]["must_not_create_line_item_from_free_text"] is True

    def test_shipping_address_message_does_not_capture_product_mentions(self) -> None:
        msg = "العنوان حفر الباطن الذيبية العنوان الوطني CMJA5515"
        assert is_shipping_address_capture_context(msg) is True
        state = MerchantConversationState(
            stage="checkout",
            order_prep=OrderPreparationState(
                missing_fields=["delivery_address"],
                line_items=[],
            ),
        )
        cart, _, changed = maybe_apply_cart_message(
            state=state,
            prep=state.order_prep,
            message=msg,
            product_info={"title": "عسل سمر", "id": "x"},
        )
        assert changed is False
        assert cart == []
        assert state.order_prep.line_items == []
        assert state.order_prep.product_mentions == []

    def test_repeated_product_mentions_do_not_duplicate_line_items(self) -> None:
        state = MerchantConversationState(stage="checkout")
        prep = OrderPreparationState()
        for msg in ("ربع كيلو سمر", "سمر", "ربع كيلو"):
            maybe_apply_cart_message(state=state, prep=prep, message=msg, product_info=None)
        assert prep.line_items == []
        assert len(prep.product_mentions) == 1

    def test_current_product_focus_not_synced_as_line_item_without_catalog_evidence(self) -> None:
        prep = {
            "product_id": "orphan-id",
            "quantity": 1,
        }
        brain = {
            "current_product_focus": {
                "id": "orphan-id",
                "title": "عسل سمر",
                "price": 250,
            },
        }
        items, _, _ = build_line_items_from_order_prep(
            order_prep=prep,
            brain_state=brain,
        )
        assert items == []


class TestCatalogOrderStillWorks:
    def test_catalog_order_still_creates_authoritative_line_items(self) -> None:
        prep = {
            "catalog_line_items_authoritative": True,
            "line_items": [
                {
                    "product_retailer_id": "sku-123",
                    "product_name": "عسل",
                    "quantity": 2,
                    "unit_price": 97.5,
                    "source": "whatsapp_native_catalog_order",
                    "from_native_catalog_order": True,
                    "match_status": "confirmed",
                }
            ],
        }
        items = authoritative_line_items_from_prep(prep)
        assert len(items) == 1
        assert line_item_has_catalog_evidence(items[0]) is True
        assert items[0]["quantity"] == 2

        nav = resolve_commerce_navigator(
            message="",
            order_prep=prep,
            inbound_metadata=_catalog_meta(),
        )
        assert nav.stage == "whatsapp_quick_order"
        assert nav.known_fields.get("product") == "known"


class TestOnlineStoreAndAddressNavigator:
    def test_existing_online_store_order_message_does_not_create_whatsapp_line_items(
        self,
    ) -> None:
        state = MerchantConversationState(stage="checkout")
        prep = OrderPreparationState()
        cart, _, changed = maybe_apply_cart_message(
            state=state,
            prep=prep,
            message="طلبت من المتجر الإلكتروني وعندي طلب قائم",
            product_info=None,
        )
        assert changed is False
        assert cart == []
        nav = resolve_commerce_navigator(message="طلبت من المتجر الإلكتروني")
        assert nav.forbidden_actions
        assert "do_not_create_whatsapp_line_items_from_text" in nav.forbidden_actions

    def test_address_correction_keeps_stage_shipping_not_browse(self) -> None:
        msg = "عشان لو طلع لك العنوان فيه لخبطة هذا العنوان الصحيح"
        nav = resolve_commerce_navigator(
            message=msg,
            order_prep=OrderPreparationState(missing_fields=["delivery_address", "city"]),
            stage="checkout",
        )
        assert nav.next_goal == "collect_or_confirm_delivery_address"
        assert "do_not_browse" in nav.forbidden_actions
        assert "do_not_push_product_list" in nav.forbidden_actions
        assert nav.stage != "browse"


class TestNeedsReviewNotReady:
    def test_needs_review_free_text_product_not_counted_as_ready_order(self) -> None:
        prep = OrderPreparationState(
            product_mentions=[
                {
                    "product_mention": "ربع كيلو سمر",
                    "catalog_match_status": "needs_review",
                    "must_not_create_line_item_from_free_text": True,
                }
            ],
            line_items=[
                {
                    "product_name": "عسل سمر",
                    "quantity": 1,
                    "source": "whatsapp_brain",
                    "match_status": "needs_review",
                }
            ],
        )
        assert order_has_authoritative_products(prep) is False
        assert authoritative_line_items_from_prep(prep) == []
        from core.order_missing_fields_engine import _has_product  # noqa: PLC0415

        ctx = SimpleNamespace(
            brain_order_prep=prep.to_dict(),
            active_draft=None,
            catalog_order=SimpleNamespace(
                has_catalog_order=False,
                product_items=[],
                total_price=None,
            ),
        )
        has_product, source = _has_product(ctx)
        assert has_product is False
        assert source == "free_text_product_mention_only"
