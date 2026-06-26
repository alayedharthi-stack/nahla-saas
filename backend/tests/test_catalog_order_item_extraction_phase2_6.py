"""Phase 2.6 — catalog_order text extraction must not ask product/quantity."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402

_LIVE_CATALOG_TEXT = """📎 رسالة من تطبيق الجوال — صيغة غير مدعومة
[طلب كتالوج من العميل]
عدد أسطر الطلب: 2
إجمالي الكمية: 2
الإجمالي: 365.5 SAR
رمز المنتج (SKU): ctv068l2de
ملاحظة: العميل أرسل طلبًا من كتالوج واتساب. تعامل معه كنية شراء، واسأله فقط عن البيانات الناقصة لإكمال الطلب.
"""

_FORBIDDEN_PRODUCT_QTY_ASKS = (
    "وش المنتج",
    "وش العدد",
    "حدد المنتج",
    "حدد المنتج أو الكمية",
    "المنتج أو الكمية",
)


def _catalog_ctx(message: str = _LIVE_CATALOG_TEXT) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=message,
        intent=Intent(name="general", confidence=0.7, raw_message=message),
        state=MerchantConversationState(stage="ordering", turn=1, greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": {}},
    )


def _resolved_items(*, product_name: str = "عسل سدر") -> SimpleNamespace:
    return SimpleNamespace(
        line_items=[{
            "product_id": "11",
            "variant_id": "22",
            "product_name": product_name,
            "title": product_name,
            "quantity": 2,
            "catalog_price": 182.75,
            "price": 182.75,
            "currency": "SAR",
            "product_retailer_id": "ctv068l2de",
            "source": "whatsapp_native_catalog_order",
        }],
        matched_count=1,
        unmatched_count=0,
        needs_review_count=0,
        price_mismatch_count=0,
    )


def _unresolved_items() -> SimpleNamespace:
    return SimpleNamespace(
        line_items=[{
            "product_name": "ctv068l2de",
            "title": "ctv068l2de",
            "quantity": 2,
            "price": 182.75,
            "currency": "SAR",
            "product_retailer_id": "ctv068l2de",
            "match_status": "needs_review",
            "source": "whatsapp_native_catalog_order",
        }],
        matched_count=0,
        unmatched_count=1,
        needs_review_count=1,
        price_mismatch_count=0,
    )


class TestCatalogOrderContractFacts:
    def test_text_marker_sets_facts_and_filters_product_quantity_missing(self) -> None:
        contract = build_commerce_turn_contract(_catalog_ctx(), db=None)

        assert contract.known_facts.get("catalog_order_current_turn") is True
        assert contract.known_facts.get("quantity_known") is True
        assert contract.known_facts.get("quantity") == 2
        assert contract.known_facts.get("catalog_order_line_count") == 2
        assert contract.known_facts.get("catalog_total") == 365.5
        assert contract.known_facts.get("catalog_skus") == ["ctv068l2de"]
        assert "do_not_ask_product" in contract.forbidden_actions
        assert "do_not_ask_quantity" in contract.forbidden_actions
        assert not {"product", "products", "product_id", "variant", "quantity", "qty"} & set(contract.missing_fields)

    def test_raw_product_or_quantity_ask_is_replaced_by_catalog_fallback(self) -> None:
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_LLM_REPLY,
            args={"reply": "حاضر، باقي تحدد المنتج أو الكمية عشان نكمل الطلب. وش العدد تبغى؟"},
        )

        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)

        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "catalog_order_extraction_incomplete"
        assert not any(phrase in str(enforced.args) for phrase in _FORBIDDEN_PRODUCT_QTY_ASKS)


class TestOrderFlowV2CatalogTextExtraction:
    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_text_sku_is_passed_to_catalog_resolver_and_not_asked_again(
        self,
        _load,
        _items,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        _load.return_value = (None, {})
        _items.return_value = _resolved_items(product_name="عسل سدر فاخر")

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=33,
            customer_phone="966542980511",
            message="[طلب كتالوج من العميل]\nعدد أسطر الطلب: 1\nإجمالي الكمية: 2\nالإجمالي: 365.5 SAR\nرمز المنتج (SKU): ctv068l2de",
            inbound_metadata={},
        )

        payload = _items.call_args.args[2]
        assert payload.items[0].product_retailer_id == "ctv068l2de"
        assert payload.items[0].quantity == 2
        assert result.handled
        assert result.reason == "catalog_order_start"
        assert result.state_patch["line_items"][0]["product_name"] == "عسل سدر فاخر"
        assert result.state_patch.get("product_id") == "11"
        assert not any(phrase in result.reply for phrase in _FORBIDDEN_PRODUCT_QTY_ASKS)

    @patch("modules.ai.order_flow_v2.owner.build_line_items_from_payload")
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_unresolved_or_incomplete_text_sku_stays_checkout_with_extraction_fallback(
        self,
        _load,
        _items,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        _load.return_value = (None, {})
        _items.return_value = _unresolved_items()

        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=33,
            customer_phone="966542980511",
            message=_LIVE_CATALOG_TEXT,
            inbound_metadata={},
        )

        assert result.handled
        assert result.reason == "catalog_order_extraction_incomplete"
        assert result.state_patch.get("order_flow_v2_active") is True
        assert result.state_patch.get("catalog_order_extraction_incomplete") is True
        assert "تفاصيل الأصناف لم تظهر كاملة" in result.reply
        assert "ctv068l2de" in result.reply
        assert "365.5" in result.reply
        assert "لم أجد أي طلبات" not in result.reply
        assert not any(phrase in result.reply for phrase in _FORBIDDEN_PRODUCT_QTY_ASKS)

    def test_normal_browse_without_catalog_order_is_unaffected(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="وش عندكم منتجات؟",
            intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم منتجات؟"),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True, orderable=True),
            profile={"inbound_metadata": {}},
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "عسل"})

        assert contract.known_facts.get("catalog_order_current_turn") is not True
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw) is raw
