"""Purchase channel button labels and non-duplicated channel-selection body."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    build_channel_choice_buttons,
    build_channel_choice_prompt,
    compose_purchase_channel_selection_goal,
)


def _three_channel_caps() -> CheckoutChannelCapabilities:
    return CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=True,
        showroom_visit=True,
        store_url="https://shop.example",
    )


def test_online_store_button_label_is_electronic_store() -> None:
    caps = _three_channel_caps()
    buttons = build_channel_choice_buttons(caps)
    store_button = next(
        b for b in buttons if b["reply"]["id"] == "checkout_store_link"
    )
    assert store_button["reply"]["title"] == "المتجر الإلكتروني"


def test_channel_selection_with_buttons_does_not_duplicate_numbered_options_in_body() -> None:
    caps = _three_channel_caps()
    body = build_channel_choice_prompt(caps, include_numbered_options=False)
    assert body == "كيف تحب تكمل؟"
    assert "1-" not in body
    assert "2-" not in body
    assert "طلب سريع عبر واتساب" not in body
    assert "المتجر الإلكتروني" not in body
    assert "زيارة المعرض" not in body


def test_channel_selection_without_buttons_can_still_describe_options_naturally() -> None:
    caps = _three_channel_caps()
    body = build_channel_choice_prompt(caps, include_numbered_options=True)
    assert body.startswith("كيف تحب تكمل؟")
    assert "1- طلب سريع عبر واتساب" in body
    assert "2- الطلب من المتجر الإلكتروني" in body
    assert "3- زيارة المعرض" in body

    llm_goal = compose_purchase_channel_selection_goal(buttons_will_render=False)
    assert "numbered list" in llm_goal


def test_purchase_channel_buttons_still_include_three_channels() -> None:
    caps = _three_channel_caps()
    buttons = build_channel_choice_buttons(caps)
    assert len(buttons) == 3
    assert [b["reply"]["id"] for b in buttons] == [
        "checkout_whatsapp_fast",
        "checkout_store_link",
        "checkout_showroom_visit",
    ]
