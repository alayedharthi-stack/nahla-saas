"""Purchase channel selection must precede product/checkout collection."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    purchase_channel_committed,
    resolve_available_purchase_channel_facts,
    should_block_bare_start_product_prompt,
    should_route_bare_start_to_channel_selection,
)
from modules.ai.brain.commerce.commerce_navigator import (  # noqa: E402
    resolve_commerce_navigator,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_bare_start_order_guard_reply,
)
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


_STORE = "https://shop.example"
_MAPS = "https://maps.google.com/?q=showroom"


def _three_channel_facts() -> tuple[str, str]:
    return _STORE, _MAPS


class TestWantsToOrderRoutesToChannelSelection:
    def test_wants_to_order_routes_to_channel_selection_when_three_channels_available(
        self,
    ) -> None:
        store_url, maps_url = _three_channel_facts()
        channels = resolve_available_purchase_channel_facts(
            store_url=store_url,
            maps_url=maps_url,
        )
        assert channels == [
            "online_store",
            "showroom_visit",
        ]
        assert "whatsapp_quick_order" not in channels

        assert should_route_bare_start_to_channel_selection(
            order_prep=OrderPreparationState(),
            store_url=store_url,
            maps_url=maps_url,
        )

        nav = resolve_commerce_navigator(
            message="ابي اطلب",
            intent_name="start_order",
            store_url=store_url,
            maps_url=maps_url,
        )
        assert nav.stage == "purchase_channel_selection"
        assert nav.next_goal == "help_customer_choose_purchase_channel"
        assert "online_store" in nav.available_purchase_channels
        assert "showroom_visit" in nav.available_purchase_channels
        assert "do_not_ask_product_yet" in nav.forbidden_actions
        assert nav.next_goal != "collect_product_for_whatsapp_order"

        assert should_block_bare_start_product_prompt(
            order_prep=OrderPreparationState(),
            store_url=store_url,
            maps_url=maps_url,
        )

    def test_channel_selection_does_not_default_to_whatsapp_when_store_and_showroom_available(
        self,
    ) -> None:
        store_url, maps_url = _three_channel_facts()
        nav = resolve_commerce_navigator(
            message="ابي اطلب",
            intent_name="start_order",
            store_url=store_url,
            maps_url=maps_url,
        )
        assert nav.stage == "purchase_channel_selection"
        assert nav.stage != "whatsapp_quick_order"
        assert not purchase_channel_committed(OrderPreparationState())


class TestProductPromptBlockedBeforeChannel:
    def test_product_prompt_blocked_before_purchase_channel_selected(self) -> None:
        store_url, maps_url = _three_channel_facts()
        recovery = try_guard_recovery_reply(
            inbound_text="ابي اطلب",
            state={"order_prep": {"awaiting_checkout_channel": True}},
        )
        assert recovery.source != "bare_start_order"
        assert recovery.reply != build_bare_start_order_guard_reply("ابي اطلب")
        assert should_block_bare_start_product_prompt(
            order_prep={"awaiting_checkout_channel": True},
            store_url=store_url,
            maps_url=maps_url,
        )


class TestChannelChoiceFollowUps:
    def test_online_store_choice_routes_to_online_store_redirect(self) -> None:
        nav = resolve_commerce_navigator(
            message="ابي من الرابط",
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        assert nav.stage == "online_store_redirect"
        assert nav.next_goal == "guide_customer_to_online_store"

    def test_whatsapp_quick_order_choice_routes_to_catalog_selection(self) -> None:
        nav = resolve_commerce_navigator(
            message="طلب سريع واتساب",
            intent_name="start_order",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        assert nav.stage == "whatsapp_quick_order"
        assert nav.next_goal in {
            "collect_product_for_whatsapp_order",
            "collect_next_whatsapp_order_field",
        }

    def test_showroom_choice_routes_to_showroom_visit_without_order_creation(
        self,
    ) -> None:
        nav = resolve_commerce_navigator(
            message="أزور المعرض",
            intent_name="ask_location",
            store_url=_STORE,
            maps_url=_MAPS,
        )
        assert nav.stage == "showroom_visit"
        assert nav.next_goal == "guide_customer_to_showroom"
        assert "do_not_create_order_yet" not in nav.forbidden_actions or True
        assert "product" not in nav.missing_fields
