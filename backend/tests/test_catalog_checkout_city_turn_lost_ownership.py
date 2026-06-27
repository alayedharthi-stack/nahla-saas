from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import is_active_catalog_checkout  # noqa: E402
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.types import BrainContext, CommerceFacts, Decision, Intent, MerchantConversationState  # noqa: E402
from modules.ai.order_flow_v2.owner import try_handle_order_flow_v2  # noqa: E402
from modules.ai.order_flow_v2.replies import build_catalog_order_start_reply  # noqa: E402
from modules.ai.order_flow_v2.state import prep_dict  # noqa: E402


_BROWSE_MARKERS = (
    "المتوفر حاليًا",
    "المتوفر حاليا",
    "تحب أعرض لك الأسعار",
    "وش المنتج اللي تبي",
)


def _line_items() -> list[dict]:
    return [
        {
            "product_id": "p250",
            "product_name": "250 جرام عسل سمر الحجاز",
            "quantity": 1,
            "catalog_price": 126.0,
            "item_price": 126.0,
            "currency": "SAR",
            "price_source": "whatsapp_catalog",
            "from_native_catalog_order": True,
            "source": "whatsapp_native_catalog_order",
        },
        {
            "product_id": "p500",
            "product_name": "500 جرام عسل سمر الحجاز",
            "quantity": 1,
            "catalog_price": 193.0,
            "item_price": 193.0,
            "currency": "SAR",
            "price_source": "whatsapp_catalog",
            "from_native_catalog_order": True,
            "source": "whatsapp_native_catalog_order",
        },
    ]


def _catalog_meta() -> dict:
    return {
        "source_type": "catalog_order",
        "product_items": [
            {"product_retailer_id": "sku250", "quantity": 1, "item_price": 126, "currency": "SAR"},
            {"product_retailer_id": "sku500", "quantity": 1, "item_price": 193, "currency": "SAR"},
        ],
        "total_price": 319,
        "currency": "SAR",
    }


def _active_ctx(message: str, order_prep: dict) -> BrainContext:
    state = MerchantConversationState(stage="ordering", turn=5, greeted=True)
    state.order_prep = type("Prep", (), {})()
    for key, value in order_prep.items():
        setattr(state.order_prep, key, value)
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000000",
        message=message,
        raw_message=message,
        intent=Intent(name="general", confidence=0.85, raw_message=message),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": {}},
    )


class TestCatalogCheckoutCityTurnLostOwnership:
    def test_catalog_order_then_city_is_consumed_without_browse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)

        brain_state = {
            "order_prep": {
                "customer_first_name": "عميل",
                "customer_last_name": "واتساب",
            },
        }

        def fake_load(_db, *, tenant_id: int, phone: str):
            return None, brain_state

        def fake_apply(_db, *, tenant_id: int, phone: str, state_patch: dict) -> bool:
            order_prep = dict(brain_state.get("order_prep") or {})
            order_prep.update(state_patch)
            brain_state["order_prep"] = order_prep
            brain_state["cart_items"] = list(order_prep.get("line_items") or [])
            return True

        with (
            patch("modules.ai.order_flow_v2.owner._load_brain_state", side_effect=fake_load),
            patch("modules.ai.order_flow_v2.owner.apply_state_patch", side_effect=fake_apply),
            patch(
                "modules.ai.order_flow_v2.owner.build_line_items_from_payload",
                return_value=SimpleNamespace(line_items=_line_items(), unmatched_count=0),
            ),
        ):
            first = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=33,
                customer_phone="966500000000",
                message="[طلب كتالوج من العميل]",
                inbound_metadata=_catalog_meta(),
            )
            assert first.handled
            assert first.reason == "catalog_order_start"
            assert "وش المدينة؟" in first.reply
            assert not any(marker in first.reply for marker in _BROWSE_MARKERS)
            fake_apply(None, tenant_id=33, phone="966500000000", state_patch=first.state_patch)

            order_prep = brain_state["order_prep"]
            assert order_prep["order_flow_v2_active"] is True
            assert len(order_prep["line_items"]) == 2
            assert order_prep["order_flow_v2_last_field"] == "city"
            assert order_prep["order_flow_v2_contract"]["field"] == "city"

            second = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=33,
                customer_phone="966500000000",
                message="مكة بطحاء قريش",
            )
            assert second.handled
            assert not any(marker in second.reply for marker in _BROWSE_MARKERS)
            assert second.state_patch["city"]
            assert second.state_patch["address_line"] == "بطحاء قريش"
            assert second.state_patch["order_flow_v2_contract"]["reason"] == "city_owned_turn"

    def test_uncertain_city_text_stays_in_checkout_clarification(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        prep = {
            "order_flow_v2_active": True,
            "order_flow_v2_trusted_price": True,
            "catalog_line_items_authoritative": True,
            "line_items": _line_items(),
            "customer_first_name": "عميل",
            "customer_last_name": "واتساب",
            "order_flow_v2_last_field": "city",
        }

        with patch("modules.ai.order_flow_v2.owner._load_brain_state", return_value=(None, {"order_prep": prep})):
            result = try_handle_order_flow_v2(
                MagicMock(),
                tenant_id=33,
                customer_phone="966500000000",
                message="بطحاء قريش",
            )

        assert result.handled
        assert "وش المدينة؟" in result.reply
        assert not any(marker in result.reply for marker in _BROWSE_MARKERS)
        assert result.state_patch["order_flow_v2_contract"]["reason"] == "city_uncertain_before_checkout"

    def test_active_checkout_raw_browse_decision_is_overridden(self) -> None:
        prep = {
            "catalog_line_items_authoritative": True,
            "catalog_checkout_total": 319.0,
            "line_items": _line_items(),
            "missing_fields": ["city", "delivery_address"],
            "order_flow_v2_last_field": "city",
        }
        ctx = _active_ctx("مكة بطحاء قريش", prep)
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"})
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)

        assert contract.known_facts.get("active_catalog_checkout") is True
        assert contract.known_facts.get("line_items_known") is True
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_order_prep_survives_after_catalog_order_start(self) -> None:
        prep = {
            "customer_first_name": "عميل",
            "customer_last_name": "واتساب",
            "catalog_line_items_authoritative": True,
            "catalog_checkout_total": 319.0,
            "line_items": _line_items(),
            "missing_fields": ["city", "delivery_address"],
            "order_flow_v2_last_field": "city",
        }
        reply = build_catalog_order_start_reply(order_prep=prep, brain_state={"cart_items": prep["line_items"]}, missing_fields=["city"])
        ctx = _active_ctx("مكة بطحاء قريش", prep)

        assert "وش المدينة؟" in reply
        assert is_active_catalog_checkout(ctx) is True
        assert len(prep_dict(ctx.state.order_prep).get("line_items") or []) == 2
        assert prep_dict(ctx.state.order_prep).get("order_flow_v2_last_field") == "city"
        assert "product" not in list(prep.get("missing_fields") or [])

    def test_normal_browse_without_active_checkout_unaffected(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966500000000",
            message="وش المتوفر؟",
            raw_message="وش المتوفر؟",
            intent=Intent(name="general", confidence=0.75, raw_message="وش المتوفر؟"),
            state=MerchantConversationState(stage="discovery", greeted=True),
            facts=CommerceFacts(has_products=True, orderable=True),
            profile={"inbound_metadata": {}},
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"})

        assert contract.known_facts.get("active_catalog_checkout") is not True
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw) is raw
