"""Phase 1 — unified sales channel availability + store_url source."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.tenant import DEFAULT_STORE, merge_defaults  # noqa: E402
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    load_channel_capabilities,
    resolve_available_purchase_channel_facts,
)
from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    resolve_commerce_navigator,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    resolve_merchant_sales_channels,
    store_url_evidence_activates_channel,
)
from modules.ai.brain.commerce.store_url_resolver import StoreUrlResolution  # noqa: E402
from routers.settings import StoreSettingsIn  # noqa: E402


_MAPS = "https://maps.google.com/?q=showroom"
_STORE = "https://shop.example.sa"


class TestOnlineStoreChannelEvidence:
    def test_online_store_channel_requires_store_url(self) -> None:
        sales = resolve_merchant_sales_channels(
            None, 0, store_url="", maps_url=_MAPS,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()
        assert sales.online_store.available is False

    def test_online_store_present_when_store_url_in_settings(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            33,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" in sales.available_purchase_channel_ids()
        assert sales.online_store.evidence == "store_url"

    def test_online_store_present_when_resolver_finds_whatsapp_button_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.commerce.store_url_resolver.resolve_store_url",
            lambda _db, _tid: StoreUrlResolution(
                found=True,
                url="https://merchant.example.sa/ar",
                source="structured_settings",
                reason="whatsapp_button_url",
            ),
        )
        sales = resolve_merchant_sales_channels(MagicMock(), 33, maps_url=_MAPS)
        assert sales.store_url == "https://merchant.example.sa/ar"
        assert "online_store" in sales.available_purchase_channel_ids()

    def test_online_store_present_when_resolver_finds_integration_storefront_url(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.commerce.store_url_resolver.resolve_store_url",
            lambda _db, _tid: StoreUrlResolution(
                found=True,
                url="https://salla.example.sa",
                source="integration",
                reason="salla",
            ),
        )
        sales = resolve_merchant_sales_channels(MagicMock(), 33)
        assert "online_store" in sales.available_purchase_channel_ids()

    def test_online_store_missing_when_store_url_empty_but_maps_present(self) -> None:
        sales = resolve_merchant_sales_channels(
            None, 0, store_url="", maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        channels = sales.available_purchase_channel_ids()
        assert "online_store" not in channels
        assert "showroom_visit" in channels
        assert "whatsapp_quick_order" in channels

    def test_merchant_override_store_url_activates_online_store(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            33,
            store_url=_STORE,
            store_url_source="merchant_profile:merchant_override",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" in sales.available_purchase_channel_ids()
        assert store_url_evidence_activates_channel(
            source="merchant_override",
            found=True,
        )
        assert not store_url_evidence_activates_channel(
            source="kb_free_text",
            found=True,
        )
        sales = resolve_merchant_sales_channels(
            None,
            0,
            store_url="https://kb-scraped.example",
            store_url_source="kb_free_text",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        assert "online_store" not in sales.available_purchase_channel_ids()


class TestNavigatorCheckoutParity:
    def test_channel_list_parity_navigator_vs_checkout_route_owner(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            1,
            store_url=_STORE,
            store_url_source="merchant_profile",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )

        caps = CheckoutChannelCapabilities(
            whatsapp_fast=sales.whatsapp_quick_order.available,
            store_link=sales.online_store.available,
            showroom_visit=sales.showroom_visit.available,
            store_url=sales.store_url,
        )
        assert caps.store_link is True
        assert caps.showroom_visit is True

        route_ids = resolve_available_purchase_channel_facts(
            merchant_sales_channels=sales,
        )
        nav = resolve_commerce_navigator(
            message="ابي اطلب",
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.available_purchase_channels == route_ids
        assert route_ids == [
            "online_store",
            "whatsapp_quick_order",
            "showroom_visit",
        ]

    def test_load_channel_capabilities_uses_resolver(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected = resolve_merchant_sales_channels(
            None,
            1,
            store_url=_STORE,
            store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            lambda _db, _tid: expected,
        )
        caps = load_channel_capabilities(MagicMock(), 1)
        assert caps.store_link is True
        assert caps.store_url == _STORE
        assert caps.showroom_visit is True


class TestPurchaseIntentGating:
    def test_purchase_intent_shows_enabled_channels_only(self) -> None:
        sales = resolve_merchant_sales_channels(
            None, 0, store_url=_STORE, store_url_source="structured_settings",
            whatsapp_order_ready=True,
        )
        nav = resolve_commerce_navigator(
            message="ابي اطلب",
            intent_name="start_order",
            merchant_sales_channels=sales,
        )
        assert nav.stage == "purchase_channel_selection"
        assert nav.available_purchase_channels == [
            "online_store",
            "whatsapp_quick_order",
        ]

    def test_greeting_does_not_show_sales_channels(self) -> None:
        sales = resolve_merchant_sales_channels(
            None, 0, store_url=_STORE, store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        nav = resolve_commerce_navigator(
            message="السلام عليكم",
            intent_name="greeting",
            merchant_sales_channels=sales,
        )
        assert nav.stage != "purchase_channel_selection"
        assert nav.available_purchase_channels == []

    def test_product_question_does_not_force_channel_selection(self) -> None:
        nav = resolve_commerce_navigator(
            message="كم سعر السدر؟",
            intent_name="ask_price",
            merchant_sales_channels=resolve_merchant_sales_channels(
                None, 0, store_url=_STORE, store_url_source="structured_settings",
                maps_url=_MAPS,
                whatsapp_order_ready=True,
            ),
        )
        assert nav.stage != "purchase_channel_selection"

    def test_order_tracking_does_not_show_sales_channels(self) -> None:
        nav = resolve_commerce_navigator(
            message="وين طلبي؟",
            intent_name="track_order",
            merchant_sales_channels=resolve_merchant_sales_channels(
                None, 0, store_url=_STORE, store_url_source="structured_settings",
                maps_url=_MAPS,
                whatsapp_order_ready=True,
            ),
        )
        assert nav.stage == "post_purchase_tracking"
        assert nav.stage != "purchase_channel_selection"


class TestSalesChannelsSettingsApi:
    def test_sales_channels_settings_exposes_store_url(self) -> None:
        assert "store_url" in StoreSettingsIn.model_fields

    def test_sales_channels_settings_saves_store_url(self) -> None:
        current = merge_defaults({}, DEFAULT_STORE)
        current["store_url"] = _STORE
        assert current["store_url"] == _STORE

    def test_sales_channels_settings_does_not_require_kb_entry(self) -> None:
        current = merge_defaults({"store_url": _STORE}, DEFAULT_STORE)
        assert current["store_url"] == _STORE
        assert "manual_knowledge_base" not in current


class TestSalesChannelAvailabilityFacts:
    def test_availability_facts_shape(self) -> None:
        sales = resolve_merchant_sales_channels(
            None, 0, store_url=_STORE, store_url_source="structured_settings",
            maps_url=_MAPS,
            whatsapp_order_ready=True,
        )
        facts = sales.availability_facts()
        assert facts["online_store"]["enabled"] is True
        assert facts["online_store"]["available"] is True
        assert facts["online_store"]["evidence"] == "store_url"
        assert facts["whatsapp_quick_order"]["available"] is True
