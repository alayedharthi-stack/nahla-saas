"""PR-6 — WhatsApp order slot stabilization + location/maps ingestion."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_address_ingestion import (  # noqa: E402
    build_maps_url_patch,
    build_whatsapp_location_patch,
    compose_address_reply,
    is_accepted_maps_url,
    is_bare_short_address_code,
    is_city_only_address_text,
    resolve_address_state_patch,
)
from core.wa_order_lifecycle import (  # noqa: E402
    compute_wa_missing_fields,
    has_accepted_delivery_address,
    resolve_wa_order_status,
)
from core.wa_order_extraction_model import resolve_order_extraction_model  # noqa: E402
from modules.ai.brain.commerce.cart_state import (  # noqa: E402
    apply_cart_intents_to_state,
    maybe_apply_cart_message,
)
from modules.ai.brain.intent.cart_intent_extractor import (  # noqa: E402
    extract_cart_intents,
    extract_cart_intents_with_context,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402


# ── Slot stabilization ───────────────────────────────────────────────────

def test_samr_hajaz_then_kilo_sets_variant() -> None:
    intents = extract_cart_intents("عسل سمر الحجاز")
    assert intents and "سمر" in intents[0]["product_name"]
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()
    apply_cart_intents_to_state(state=state, prep=prep, intents=intents)
    follow = extract_cart_intents_with_context("كيلو", cart_items=state.cart_items)
    assert follow[0]["action"] == "update_variant"
    assert follow[0]["new_variant"] == "1kg"
    cart, _, _ = apply_cart_intents_to_state(state=state, prep=prep, intents=follow)
    assert cart[0]["variant"] == "1kg"


def test_second_kilo_confirms_quantity_not_variant_reask() -> None:
    cart = [{"product_name": "عسل سمر الحجاز", "variant": "1kg", "quantity": 1}]
    intents = extract_cart_intents_with_context("كيلو", cart_items=cart)
    assert intents[0]["action"] == "update_quantity"
    assert intents[0]["quantity"] == 1


def test_jadeed_sets_edition_without_losing_product() -> None:
    cart = [{"product_name": "عسل سمر الحجاز", "variant": "1kg", "quantity": 1}]
    intents = extract_cart_intents_with_context("الجديد", cart_items=cart)
    assert intents[0]["action"] == "update_edition"
    state = SimpleNamespace(cart_items=list(cart), current_product_focus={})
    prep = OrderPreparationState(line_items=list(cart))
    out, _, changed = apply_cart_intents_to_state(state=state, prep=prep, intents=intents)
    assert changed
    assert out[0]["product_name"] == "عسل سمر الحجاز"
    assert "جديد" in out[0]["edition"]


def test_habtain_sets_quantity_two() -> None:
    cart = [{"product_name": "عسل سمر", "variant": "1kg", "quantity": 1}]
    intents = extract_cart_intents_with_context("حبتين", cart_items=cart)
    assert intents[0]["quantity"] == 2


def test_repeated_kilo_does_not_add_duplicate_line() -> None:
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()
    apply_cart_intents_to_state(
        state=state, prep=prep,
        intents=extract_cart_intents("عسل سمر"),
    )
    apply_cart_intents_to_state(
        state=state, prep=prep,
        intents=extract_cart_intents_with_context("كيلو", cart_items=state.cart_items),
    )
    apply_cart_intents_to_state(
        state=state, prep=prep,
        intents=extract_cart_intents_with_context("كيلو", cart_items=state.cart_items),
    )
    assert len(state.cart_items) == 1
    assert state.cart_items[0]["variant"] == "1kg"
    assert state.cart_items[0]["quantity"] == 1


def test_variant_clears_awaiting_variant_choice() -> None:
    seed = [{"product_name": "عسل سمر", "quantity": 1}]
    state = SimpleNamespace(cart_items=list(seed), current_product_focus={})
    prep = OrderPreparationState(line_items=list(seed), awaiting_variant_choice=True)
    apply_cart_intents_to_state(
        state=state, prep=prep,
        intents=extract_cart_intents_with_context("كيلو", cart_items=seed),
    )
    assert prep.awaiting_variant_choice is False


# ── Location / maps ingestion ────────────────────────────────────────────

def test_whatsapp_location_patch() -> None:
    patch = build_whatsapp_location_patch({
        "latitude": 24.7136,
        "longitude": 46.6753,
        "name": "Home",
        "address": "Riyadh",
    })
    assert patch["delivery_address_type"] == "whatsapp_location"
    assert patch["delivery_location_lat"] == "24.7136"
    assert "maps.google.com" in patch["google_maps_url"]


def test_location_accepted_in_lifecycle() -> None:
    prep = build_whatsapp_location_patch({
        "latitude": 24.7,
        "longitude": 46.6,
        "address": "x",
    })
    assert has_accepted_delivery_address(prep)


def test_location_removes_delivery_address_from_missing() -> None:
    prep = {
        **build_whatsapp_location_patch({"latitude": 24.7, "longitude": 46.6}),
        "customer_first_name": "A",
        "customer_last_name": "B",
        "city": "Riyadh",
        "line_items": [{"product_name": "x", "quantity": 1}],
    }
    missing = compute_wa_missing_fields(prep, line_items=prep["line_items"])
    assert "delivery_address" not in missing


def test_complete_order_moves_to_pending_payment_after_location() -> None:
    prep = {
        **build_whatsapp_location_patch({"latitude": 24.7, "longitude": 46.6}),
        "customer_first_name": "A",
        "customer_last_name": "B",
        "city": "Riyadh",
        "line_items": [{"product_name": "عسل", "variant": "1kg", "quantity": 1}],
    }
    status, missing, _ = resolve_wa_order_status(
        prep, {}, line_items=prep["line_items"],
    )
    assert status == "pending_payment"
    assert not missing


def test_incomplete_order_stays_pending_customer_info_after_location() -> None:
    prep = {
        **build_whatsapp_location_patch({"latitude": 24.7, "longitude": 46.6}),
        "line_items": [],
    }
    status, missing, _ = resolve_wa_order_status(prep, {})
    assert status is None
    assert "product" in compute_wa_missing_fields(prep, line_items=[])


def test_compose_reply_complete_vs_incomplete() -> None:
    from core.merchant_payment_methods import resolve_merchant_payment_methods  # noqa: PLC0415

    bank_only = resolve_merchant_payment_methods(
        extra_metadata={"payment_methods": {"bank_transfer_enabled": True, "cash_on_delivery_enabled": False}},
    )
    complete_prep = {
        **build_whatsapp_location_patch({"latitude": 24.7, "longitude": 46.6}),
        "customer_first_name": "A",
        "customer_last_name": "B",
        "city": "Riyadh",
        "line_items": [{"product_name": "x", "quantity": 1}],
    }
    reply = compose_address_reply(
        order_prep=complete_prep,
        brain_state={},
        line_items=complete_prep["line_items"],
        payment_methods=bank_only,
    )
    assert "طريقة الدفع" in reply
    assert "تحويل بنكي" in reply
    incomplete = build_whatsapp_location_patch({"latitude": 24.7, "longitude": 46.6})
    assert "المنتج" in compose_address_reply(
        order_prep=incomplete, brain_state={},
    )


def test_google_maps_url_accepted() -> None:
    url = "https://maps.google.com/?q=24.7,46.6"
    assert is_accepted_maps_url(url)
    patch = build_maps_url_patch(url)
    assert patch["delivery_address_type"] == "maps_url"
    assert patch["google_maps_url"]


def test_apple_maps_url_accepted() -> None:
    url = "https://maps.apple.com/?ll=24.7,46.6"
    assert is_accepted_maps_url(url)


def test_city_only_not_accepted() -> None:
    assert is_city_only_address_text("الرياض")
    assert resolve_address_state_patch(
        inbound_normalized_type="text", inbound_text="الرياض",
    ) is None


def test_short_address_code_accepted() -> None:
    assert is_bare_short_address_code("RQWB3094")
    patch = resolve_address_state_patch(
        inbound_normalized_type="text",
        inbound_text="RQWB3094",
    )
    assert patch is not None
    assert patch["short_address_code"] == "RQWB3094"
    assert patch["delivery_address_type"] == "short_address_code"


def test_maps_url_not_payment_path_guard() -> None:
    """Maps URLs resolve to address patch, not payment metadata."""
    patch = resolve_address_state_patch(
        inbound_normalized_type="text",
        inbound_text="https://maps.app.goo.gl/abc123",
    )
    assert patch is not None
    assert "payment" not in str(patch).lower()


def test_extraction_model_flag(monkeypatch) -> None:
    monkeypatch.setenv("NAHLA_ORDER_EXTRACTION_MODEL", "gpt-4.1")
    assert resolve_order_extraction_model() == "gpt-4.1"
    monkeypatch.delenv("NAHLA_ORDER_EXTRACTION_MODEL", raising=False)
    assert resolve_order_extraction_model(confidence=0.2) == "gpt-4.1-mini"
