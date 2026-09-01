"""AGENT-3 D1 — canonical purchase-channel availability and structured selection.

Generic commerce fixtures only. Phrases are acceptance examples, not runtime
triggers. Assert owner/state/ids — not customer-facing wording.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    apply_selected_purchase_channel,
    extract_structured_purchase_channel_id,
    resolve_explicit_purchase_channel_payload,
    resolve_purchase_channel_entry_owner,
    validate_selected_purchase_channel,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    MerchantSalesChannels,
    SalesChannelSlot,
    resolve_merchant_sales_channels,
)
from modules.ai.brain.commerce.store_url_resolver import (  # noqa: E402
    canonical_merchant_storefront_url,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SELECT_PURCHASE_CHANNEL,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_STORE = "https://shop.example.sa"
_MAPS = "https://maps.google.com/?q=showroom"
_PHONE_A = "966500000011"
_PHONE_B = "966500000012"


def _slot(*, enabled: bool, available: bool, evidence: str) -> SalesChannelSlot:
    return SalesChannelSlot(enabled=enabled, available=available, evidence=evidence)


def _sales(
    *,
    store: bool = False,
    whatsapp: bool = False,
    showroom: bool = False,
    store_url: str = "",
    maps_url: str = "",
) -> MerchantSalesChannels:
    return MerchantSalesChannels(
        store_url=store_url,
        store_url_source="structured_settings" if store else "none",
        maps_url=maps_url,
        online_store=_slot(
            enabled=store, available=store, evidence="store_url" if store else "none"
        ),
        whatsapp_quick_order=_slot(
            enabled=whatsapp,
            available=whatsapp,
            evidence="whatsapp_catalog" if whatsapp else "whatsapp_catalog_unavailable",
        ),
        showroom_visit=_slot(
            enabled=showroom,
            available=showroom,
            evidence="maps_url" if showroom else "none",
        ),
    )


def _facts(*, store_url: str = "", maps_url: str = "") -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=12,
        in_stock_count=12,
        has_active_integration=True,
        orderable=True,
        store_name="متجر تجريبي عام",
        store_url=store_url,
        maps_url=maps_url,
        store_url_source="structured_settings" if store_url else "none",
    )


def _awaiting_state(
    *,
    offered: list[str] | None = None,
    channel: str = "",
) -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True,
        stage="purchase_channel_selection",
        turn=3,
        order_prep=OrderPreparationState(
            awaiting_checkout_channel=True,
            checkout_channel=channel,
            offered_purchase_channel_ids=list(
                offered
                or ["online_store", "whatsapp_quick_order", "showroom_visit"]
            ),
        ),
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = 11,
    phone: str = _PHONE_A,
    intent_name: str = "general",
    sales: MerchantSalesChannels | None = None,
    state: MerchantConversationState | None = None,
    inbound_metadata: dict[str, Any] | None = None,
    intent_slots: dict[str, Any] | None = None,
    store_url: str = _STORE,
    maps_url: str = _MAPS,
) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=msg,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=msg,
            slots=dict(intent_slots or {}),
        ),
        state=state or MerchantConversationState(greeted=True, stage="discovery", turn=2),
        facts=_facts(store_url=store_url, maps_url=maps_url),
    )
    if sales is not None:
        ctx.merchant_sales_channels = sales  # type: ignore[attr-defined]
    if inbound_metadata is not None:
        ctx.inbound_metadata = inbound_metadata  # type: ignore[attr-defined]
    return ctx


def _decide(ctx: BrainContext):
    with patch(
        "modules.ai.brain.commerce.commerce_entry_catalog_delivery.try_commerce_entry_catalog_decision",
        return_value=None,
    ):
        return DefaultDecisionEngine().decide(ctx)


class TestAvailabilityA1A8:
    def test_a1_three_channels(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert sales.available_purchase_channel_ids() == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_a2_online_and_whatsapp_omit_showroom(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url="",
            whatsapp_order_ready=True,
        )
        assert sales.available_purchase_channel_ids() == [
            "online_store",
            "whatsapp_quick_order",
        ]
        assert "showroom_visit" not in sales.available_purchase_channel_ids()

    def test_a3_showroom_only_direct_owner(self) -> None:
        sales = _sales(showroom=True, maps_url=_MAPS)
        owner = resolve_purchase_channel_entry_owner(
            message="ابي اطلب",
            intent=Intent(name="start_order", confidence=0.9, raw_message="ابي اطلب"),
            merchant_sales_channels=sales,
        )
        assert owner == "showroom_visit"
        ctx = _ctx(
            "ابي اطلب",
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url=_MAPS,
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "showroom_visit"
        assert decision.args.get("topic") != "purchase_channel_selection"

    def test_a4_missing_online_url(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url="",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()
        assert sales.store_url == ""

    def test_a5_malformed_online_url(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url="not a url",
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()
        assert sales.store_url == ""
        assert canonical_merchant_storefront_url("not a url") == ""
        assert canonical_merchant_storefront_url("http://") == ""
        assert canonical_merchant_storefront_url("ftp://shop.example.sa") == ""
        assert canonical_merchant_storefront_url("https://app.nahlah.ai/register") == ""

    def test_a6_whatsapp_enabled_capability_unavailable(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=False,
        )
        assert sales.whatsapp_quick_order.enabled is True
        assert sales.whatsapp_quick_order.available is False
        assert "whatsapp_quick_order" not in sales.available_purchase_channel_ids()

    def test_a7_whatsapp_only_ready_direct_owner(self) -> None:
        sales = _sales(whatsapp=True)
        ctx = _ctx(
            "ابي اطلب",
            intent_name="start_order",
            sales=sales,
            store_url="",
            maps_url="",
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("topic") == "whatsapp_quick_order"
        assert decision.args.get("available_purchase_channels") == [
            "whatsapp_quick_order",
        ]

    def test_a8_showroom_missing_valid_location(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            11,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url="not a maps url",
            whatsapp_order_ready=True,
        )
        assert "showroom_visit" not in sales.available_purchase_channel_ids()
        assert sales.maps_url == ""


class TestSemanticSelectionB1B10:
    def test_b1_structured_online_store(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "طيب المتجر الإلكتروني",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
            intent_slots={"selected_channel_id": "online_store"},
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "online_store"

    def test_b2_structured_showroom(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "أجي المعرض",
            intent_name="ask_location",
            sales=sales,
            state=_awaiting_state(),
            intent_slots={"selected_channel_id": "showroom_visit"},
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "showroom_visit"

    def test_b3_structured_whatsapp(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "خلنا نكمل من الواتساب",
            intent_name="start_order",
            sales=sales,
            state=_awaiting_state(),
            inbound_metadata={
                "action": "select_purchase_channel",
                "selected_channel_id": "whatsapp_quick_order",
            },
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "whatsapp_quick_order"

    def test_b4_ambiguous_wording_does_not_guess(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        ctx = _ctx(
            "مو متأكد",
            intent_name="general",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "purchase_channel_selection"
        assert decision.args.get("selected_channel_id") in {None, ""}

    def test_b5_min_hunak_does_not_select_whatsapp(self) -> None:
        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        phrase = "من هناك"
        assert extract_structured_purchase_channel_id(message=phrase) is None
        ctx = _ctx(
            phrase,
            intent_name="general",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") not in {
            "whatsapp_quick_order",
            "online_store",
            "showroom_visit",
        }
        assert decision.args.get("topic") == "purchase_channel_selection"

    def test_b6_selected_not_offered_rejected(self) -> None:
        result = validate_selected_purchase_channel(
            selected_channel_id="showroom_visit",
            tenant_id=11,
            merchant_sales_channels=_sales(
                store=True, whatsapp=True, store_url=_STORE
            ),
            offered_purchase_channel_ids=["online_store", "whatsapp_quick_order"],
        )
        assert result.accepted is False
        assert result.reason == "channel_not_offered"

    def test_b7_channel_unavailable_before_execution_rejected(self) -> None:
        result = validate_selected_purchase_channel(
            selected_channel_id="online_store",
            tenant_id=11,
            merchant_sales_channels=_sales(whatsapp=True),
            offered_purchase_channel_ids=["online_store", "whatsapp_quick_order"],
        )
        assert result.accepted is False
        assert result.reason == "channel_unavailable"

    def test_b8_exact_button_chrome_still_works(self) -> None:
        from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
            CheckoutChannelCapabilities,
        )

        sales = _sales(store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS)
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True, store_link=True, showroom_visit=True, store_url=_STORE
        )
        assert resolve_explicit_purchase_channel_payload(
            "المتجر الإلكتروني",
            caps=caps,
        ) == CHECKOUT_CHANNEL_STORE
        assert resolve_explicit_purchase_channel_payload(
            "",
            caps=caps,
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
        ) == CHECKOUT_CHANNEL_WHATSAPP
        ctx = _ctx(
            "المتجر الإلكتروني",
            intent_name="ask_store_info",
            sales=sales,
            state=_awaiting_state(),
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_SELECT_PURCHASE_CHANNEL
        assert decision.args.get("selected_channel_id") == "online_store"

    def test_b9_selection_persists_through_checkout_state(self) -> None:
        conv = MagicMock()
        conv.extra_metadata = {"brain_state": {"order_prep": {"awaiting_checkout_channel": True}}}
        db = MagicMock()
        with patch(
            "core.order_flow._load_brain_state",
            return_value=(conv, {"order_prep": {"awaiting_checkout_channel": True}}),
        ):
            result = apply_selected_purchase_channel(
                db,
                tenant_id=11,
                phone=_PHONE_A,
                selected_channel_id="online_store",
                merchant_sales_channels=_sales(
                    store=True, whatsapp=True, showroom=True, store_url=_STORE, maps_url=_MAPS
                ),
                offered_purchase_channel_ids=[
                    "online_store",
                    "whatsapp_quick_order",
                    "showroom_visit",
                ],
            )
        assert result.accepted is True
        assert result.checkout_channel == CHECKOUT_CHANNEL_STORE
        op = conv.extra_metadata["brain_state"]["order_prep"]
        assert op["checkout_channel"] == CHECKOUT_CHANNEL_STORE
        assert op["awaiting_checkout_channel"] is False

    def test_b10_cross_tenant_channel_state_impossible(self) -> None:
        seen: list[int] = []

        def _resolve(db: Any, tenant_id: int, **kwargs: Any) -> MerchantSalesChannels:
            seen.append(int(tenant_id))
            if int(tenant_id) == 11:
                return _sales(store=True, store_url=_STORE)
            return _sales(whatsapp=True)

        with patch(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            side_effect=_resolve,
        ):
            other = validate_selected_purchase_channel(
                selected_channel_id="whatsapp_quick_order",
                tenant_id=11,
                db=MagicMock(),
                offered_purchase_channel_ids=["whatsapp_quick_order"],
            )
            mine = validate_selected_purchase_channel(
                selected_channel_id="online_store",
                tenant_id=11,
                db=MagicMock(),
                offered_purchase_channel_ids=["online_store"],
            )
        assert seen == [11, 11]
        assert other.accepted is False
        assert mine.accepted is True


class TestCanonicalStoreUrl:
    def test_valid_https_kept(self) -> None:
        assert canonical_merchant_storefront_url(_STORE) == _STORE

    def test_empty_rejected(self) -> None:
        assert canonical_merchant_storefront_url("") == ""
        assert canonical_merchant_storefront_url("   ") == ""
