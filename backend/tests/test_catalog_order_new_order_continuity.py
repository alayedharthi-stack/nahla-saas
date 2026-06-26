"""Native catalog order continuity after create-new-order path."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_native_catalog_order import (  # noqa: E402
    NativeCatalogOrderItem,
    NativeCatalogOrderPayload,
    apply_native_order_to_state,
)
from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    _product_from_state,
)
from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: E402
    _PROMPT_CITY,
    build_checkout_slot_fallback_reply,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER  # noqa: E402
from modules.ai.brain.execution.orders import DraftOrderHandler  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    select_arabic_commerce_fallback,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.brain.intent_priority.types import GOAL_ORDER_REQUEST  # noqa: E402

_HONEY_PRODUCT_RE = re.compile(
    r"وش\s+نوع\s+العسل|وش\s+العدد",
    re.UNICODE,
)
def _stale_checkout_state(*, missing: list[str] | None = None) -> MerchantConversationState:
    state = MerchantConversationState(stage="ordering", turn=3)
    state.order_prep = OrderPreparationState(
        order_status="awaiting_address",
        missing_fields=list(missing or ["city"]),
    )
    return state


def _catalog_ctx(*, meta: dict | None = None) -> BrainContext:
    prep = OrderPreparationState(
        product_id="100",
        quantity=2,
        catalog_line_items_authoritative=True,
        catalog_checkout_total=319.0,
        catalog_checkout_currency="SAR",
        checkout_channel="whatsapp_catalog",
        line_items=[
            {
                "product_id": "100",
                "product_retailer_id": "86bqzca62a",
                "quantity": 2,
                "unit_price": 159.5,
                "currency": "SAR",
                "from_native_catalog_order": True,
                "match_status": "confirmed",
            },
        ],
    )
    state = MerchantConversationState(stage="ordering", turn=4, order_prep=prep)
    profile = {"inbound_metadata": meta or {"source_type": "catalog_order", "product_items": [{}]}}
    return BrainContext(
        tenant_id=1,
        customer_phone="966542980511",
        message="",
        intent=Intent(name="start_order", confidence=0.9, raw_message=""),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        history=[],
        profile=profile,
    )


class TestCreateNewOrderContinuity:
    def test_yes_after_create_new_order_does_not_ask_city_before_product(self) -> None:
        state = _stale_checkout_state(missing=["city"])
        reply = build_checkout_slot_fallback_reply(state=state, inbound_text="نعم")
        assert reply != _PROMPT_CITY
        assert reply is None

    def test_wants_to_order_does_not_trigger_old_product_quantity_prompt(self) -> None:
        state = _stale_checkout_state(missing=["city"])
        fallback, kind = select_arabic_commerce_fallback(
            inbound_text="ابي اطلب",
            primary_customer_goal=GOAL_ORDER_REQUEST,
            state=state,
        )
        assert kind == "bare_start_order_no_product"
        assert fallback == ""
        assert not _HONEY_PRODUCT_RE.search(fallback)


class TestNativeCatalogAuthoritativeState:
    def test_native_catalog_order_sets_authoritative_state_flags(self) -> None:
        state = MerchantConversationState()
        payload = NativeCatalogOrderPayload(
            catalog_id="CAT-1",
            customer_note="",
            items=[
                NativeCatalogOrderItem(
                    product_retailer_id="86bqzca62a",
                    quantity=2,
                    item_price=159.5,
                    currency="SAR",
                    name="Honey",
                ),
            ],
            raw_product_items=[{"product_retailer_id": "86bqzca62a", "quantity": 2}],
        )
        db = MagicMock()
        with patch(
            "core.wa_native_catalog_order.build_line_items_from_payload",
        ) as mock_build:
            mock_build.return_value = MagicMock(
                line_items=[
                    {
                        "product_id": "100",
                        "product_retailer_id": "86bqzca62a",
                        "quantity": 2,
                        "unit_price": 159.5,
                        "currency": "SAR",
                        "from_native_catalog_order": True,
                    },
                ],
            )
            apply_native_order_to_state(db=db, tenant_id=1, state=state, payload=payload)

        prep = state.order_prep
        assert prep.catalog_line_items_authoritative is True
        assert prep.checkout_channel == "whatsapp_catalog"
        assert len(prep.line_items) == 1
        assert state.cart_items == prep.line_items
        assert prep.quantity == 2
        assert prep.catalog_checkout_total == 319.0

    def test_catalog_order_sku_is_not_treated_as_salla_external_id(self) -> None:
        ctx = _catalog_ctx()
        product = _product_from_state(ctx)
        assert product is not None
        assert product.get("product_retailer_id") == "86bqzca62a"
        assert not product.get("external_id")


class TestCatalogOrderResponderAndHandler:
    def test_catalog_order_does_not_emit_technical_fallback_when_line_items_valid(
        self,
    ) -> None:
        ctx = _catalog_ctx()
        decision = Decision(action=ACTION_PROPOSE_DRAFT_ORDER, args={})
        result = ActionResult(
            success=True,
            data={
                "product": {"title": "Honey", "from_native_catalog_order": True},
                "order_prep": ctx.state.order_prep.to_dict(),
            },
        )
        reply = asyncio.run(DefaultComposer().compose(decision, result, ctx))
        assert reply is not None
        assert "وش المنتج" not in reply
        assert "وش العدد" not in reply
        assert "وش الوزن" not in reply

    def test_catalog_order_routes_to_missing_fields_only_after_draft_creation(
        self,
    ) -> None:
        ctx = _catalog_ctx()
        ctx.state.order_prep.city = ""
        ctx.state.order_prep.customer_first_name = "Test"
        ctx.state.order_prep.customer_last_name = "User"
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "product": {
                    "id": "100",
                    "product_retailer_id": "86bqzca62a",
                    "title": "Honey",
                    "from_native_catalog_order": True,
                    "line_items": ctx.state.order_prep.line_items,
                },
                "catalog_order_submitted": True,
            },
        )
        ctx._db = MagicMock()  # type: ignore[attr-defined]

        with patch(
            "modules.ai.brain.execution.orders._ensure_product_options_loaded",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._missing_checkout_fields",
            return_value=["city"],
        ), patch(
            "modules.ai.brain.execution.orders._filter_missing_phone_if_known",
            side_effect=lambda missing, phone: missing,
        ), patch(
            "modules.ai.brain.execution.orders._resolve_checkout_address",
            new_callable=AsyncMock,
        ), patch(
            "modules.ai.brain.execution.orders._seed_checkout_state",
        ), patch(
            "modules.ai.brain.commerce.cart_state.maybe_apply_cart_message",
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        assert result.success is True
        assert result.data.get("product_unsyncable") is not True
        assert result.data.get("needs_collection") is True
        assert "city" in (result.data.get("missing_fields") or [])
