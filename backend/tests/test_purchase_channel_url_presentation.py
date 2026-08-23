"""Purchase-channel selector: elide duplicate canonical store URL from body.

Presentation/wire only. Assert structured delivery, not frozen Arabic prose.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_link_buttons import (  # noqa: E402
    prepare_purchase_channel_selector_presentation,
    whatsapp_reply_buttons_payload,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    build_channel_choice_buttons,
)

_STORE = "https://shop.example"
_STORE_SLASH = "https://shop.example/"
_OTHER = "https://pay.example/checkout"
_MAPS = "https://maps.google.com/?q=Riyadh"
_PRODUCT = "https://shop.example/products/white-sneakers"
_SOCIAL = "https://instagram.com/genericstore"
_TENANT_B_STORE = "https://other-merchant.example"


def _store_button(*, url: str = "") -> dict:
    button = {
        "type": "reply",
        "reply": {"id": "checkout_store_link", "title": "المتجر الإلكتروني"},
    }
    if url:
        button["url"] = url
    return button


def _whatsapp_button() -> dict:
    return {
        "type": "reply",
        "reply": {"id": "checkout_whatsapp_fast", "title": "طلب سريع واتساب"},
    }


def _showroom_button() -> dict:
    return {
        "type": "reply",
        "reply": {"id": "checkout_showroom_visit", "title": "زيارة المعرض"},
    }


def _two_channel_buttons(*, store_url: str = "") -> list:
    return [_whatsapp_button(), _store_button(url=store_url)]


def _prepare(
    body: str,
    *,
    buttons: list | None = None,
    topic: str = "purchase_channel_selection",
    owner: str = "",
    canonical: str = _STORE,
):
    return prepare_purchase_channel_selector_presentation(
        body=body,
        buttons=buttons if buttons is not None else _two_channel_buttons(),
        topic=topic,
        owner=owner,
        canonical_store_url=canonical,
    )


def test_a_duplicate_canonical_store_url_removed_from_body() -> None:
    body = (
        "أبشر، تقدر تطلب بإحدى الطريقتين:\n"
        f"من المتجر الإلكتروني {_STORE} أو نكمل طلبك هنا بالواتساب.\n"
        "أي طريقة تناسبك؟"
    )
    cleaned, buttons = _prepare(body)
    assert _STORE not in cleaned
    assert "https://" not in cleaned
    assert "أبشر" in cleaned
    assert buttons[1]["url"] == _STORE
    assert buttons[1]["reply"]["id"] == "checkout_store_link"


def test_b_button_payload_keeps_exact_canonical_store_url() -> None:
    cleaned, buttons = _prepare(f"اختَر الطريقة {_STORE}")
    store = next(b for b in buttons if b["reply"]["id"] == "checkout_store_link")
    assert store["url"] == _STORE
    assert cleaned.find(_STORE) == -1


def test_c_whatsapp_quick_order_button_unchanged() -> None:
    original = _two_channel_buttons()
    _cleaned, buttons = _prepare(f"النص {_STORE}", buttons=original)
    wa = next(b for b in buttons if b["reply"]["id"] == "checkout_whatsapp_fast")
    assert wa["reply"]["id"] == original[0]["reply"]["id"]
    assert wa["reply"]["title"] == original[0]["reply"]["title"]
    assert wa["type"] == "reply"
    assert "url" not in wa


def test_d_no_store_button_does_not_strip_store_url() -> None:
    body = f"رابط المتجر {_STORE}"
    cleaned, buttons = _prepare(
        body,
        buttons=[_whatsapp_button()],
    )
    assert _STORE in cleaned
    assert [b["reply"]["id"] for b in buttons] == ["checkout_whatsapp_fast"]


def test_e_unrelated_customer_facing_url_unchanged() -> None:
    body = f"المتجر {_STORE} والدفع {_OTHER}"
    cleaned, _buttons = _prepare(body)
    assert _STORE not in cleaned
    assert _OTHER in cleaned


def test_f_ordinary_catalog_reply_with_url_unchanged() -> None:
    body = f"هذا المنتج {_PRODUCT}"
    cleaned, buttons = _prepare(
        body,
        topic="catalog_browse",
        buttons=_two_channel_buttons(),
    )
    assert cleaned == body
    assert "url" not in buttons[1]


def test_g_social_faq_location_payment_links_unchanged() -> None:
    body = f"المعرض {_MAPS} وانستغرام {_SOCIAL} والدفع {_OTHER} والمتجر {_STORE}"
    cleaned, _buttons = _prepare(body)
    assert _STORE not in cleaned
    assert _MAPS in cleaned
    assert _SOCIAL in cleaned
    assert _OTHER in cleaned


def test_h_two_channel_selector_ids_and_titles_preserved() -> None:
    caps = CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=True,
        showroom_visit=False,
        store_url=_STORE,
    )
    original = list(build_channel_choice_buttons(caps))
    body = f"أي طريقة تناسبك؟ {_STORE}"
    cleaned, buttons = _prepare(body, buttons=original, canonical=_STORE)
    assert [b["reply"]["id"] for b in buttons] == [b["reply"]["id"] for b in original]
    assert [b["reply"]["title"] for b in buttons] == [
        b["reply"]["title"] for b in original
    ]
    assert _STORE not in cleaned
    assert buttons[1]["url"] == _STORE
    assert len(buttons) == 2


def test_i_three_channel_selector_ids_and_titles_preserved() -> None:
    caps = CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=True,
        showroom_visit=True,
        store_url=_STORE,
    )
    original = list(build_channel_choice_buttons(caps))
    body = f"اختر {_STORE}"
    cleaned, buttons = _prepare(body, buttons=original, canonical=_STORE)
    assert [b["reply"]["id"] for b in buttons] == [
        "checkout_whatsapp_fast",
        "checkout_store_link",
        "checkout_showroom_visit",
    ]
    assert [b["reply"]["title"] for b in buttons] == [
        b["reply"]["title"] for b in original
    ]
    assert _STORE not in cleaned
    assert buttons[1]["url"] == _STORE


def test_j_one_channel_direct_route_does_not_strip() -> None:
    body = f"نكمل من المتجر {_STORE}"
    cleaned_empty, buttons_empty = _prepare(
        body,
        topic="start_order",
        buttons=[],
        canonical=_STORE,
    )
    assert cleaned_empty == body
    assert buttons_empty == []

    cleaned_wa, buttons_wa = _prepare(
        body,
        topic="purchase_channel_selection",
        buttons=[_whatsapp_button()],
        canonical=_STORE,
    )
    assert _STORE in cleaned_wa
    assert [b["reply"]["id"] for b in buttons_wa] == ["checkout_whatsapp_fast"]


def test_k_no_hardcoded_tenant_or_salla_domain_in_helper() -> None:
    import inspect
    from core import wa_link_buttons as mod

    source = inspect.getsource(mod.prepare_purchase_channel_selector_presentation)
    payload_src = inspect.getsource(mod.whatsapp_reply_buttons_payload)
    combined = source + payload_src
    assert "salla.sa" not in combined
    assert "demostore" not in combined
    assert "Tenant 1" not in combined


def test_l_tenant_isolation_only_matching_canonical_url_is_elided() -> None:
    body = f"متجرك {_STORE} ومتجر آخر {_TENANT_B_STORE}"
    cleaned_a, buttons_a = _prepare(body, canonical=_STORE)
    assert _STORE not in cleaned_a
    assert _TENANT_B_STORE in cleaned_a
    assert buttons_a[1]["url"] == _STORE

    cleaned_b, buttons_b = _prepare(
        body,
        buttons=_two_channel_buttons(),
        canonical=_TENANT_B_STORE,
    )
    assert _TENANT_B_STORE not in cleaned_b
    assert _STORE in cleaned_b
    assert buttons_b[1]["url"] == _TENANT_B_STORE


def test_trailing_slash_variant_of_canonical_url_is_elided() -> None:
    cleaned, buttons = _prepare(f"المتجر {_STORE_SLASH}")
    assert "shop.example" not in cleaned
    assert buttons[1]["url"] == _STORE


def test_product_path_on_same_host_is_not_elided() -> None:
    body = f"المنتج {_PRODUCT} والمتجر {_STORE}"
    cleaned, _buttons = _prepare(body)
    assert _PRODUCT in cleaned
    assert cleaned.count("shop.example") == 1


def test_owner_field_also_enables_the_rule() -> None:
    cleaned, buttons = _prepare(
        f"النص {_STORE}",
        topic="",
        owner="purchase_channel_selection",
    )
    assert _STORE not in cleaned
    assert buttons[1]["url"] == _STORE


def test_mismatched_button_destination_does_not_strip() -> None:
    buttons = _two_channel_buttons(store_url="https://elsewhere.example")
    body = f"المتجر {_STORE}"
    cleaned, out = _prepare(body, buttons=buttons, canonical=_STORE)
    assert _STORE in cleaned
    assert out[1]["url"] == "https://elsewhere.example"


def test_empty_bullet_and_dangling_colon_are_tidied() -> None:
    body = f"الخيارات:\n- {_STORE}\n\nأي طريقة تناسبك؟"
    cleaned, _buttons = _prepare(body)
    assert _STORE not in cleaned
    assert "\n-" not in cleaned
    assert "أي طريقة تناسبك؟" in cleaned
    assert "  " not in cleaned


def test_meta_payload_keeps_ids_titles_and_omits_url() -> None:
    _cleaned, buttons = _prepare(f"النص {_STORE}")
    payload = whatsapp_reply_buttons_payload(buttons)
    assert [b["reply"]["id"] for b in payload] == [
        "checkout_whatsapp_fast",
        "checkout_store_link",
    ]
    assert payload[1]["reply"]["title"] == "المتجر الإلكتروني"
    assert "url" not in payload[1]
    assert buttons[1]["url"] == _STORE
