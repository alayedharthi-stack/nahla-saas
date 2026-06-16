"""Tests for PR-4 brain cart intent extraction and state application."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.cart_state import (  # noqa: E402
    apply_cart_intents_to_state,
    maybe_apply_cart_message,
)
from modules.ai.brain.intent.cart_intent_extractor import extract_cart_intents  # noqa: E402
from modules.ai.brain.types import OrderPreparationState  # noqa: E402


def test_extract_add_samr_half_kilo() -> None:
    intents = extract_cart_intents("أضف نصف كيلو سمر")
    assert len(intents) == 1
    assert intents[0]["action"] == "add_item"
    assert "سمر" in intents[0]["product_name"]
    assert intents[0]["variant"] == "500g"


def test_extract_multi_item_message() -> None:
    intents = extract_cart_intents("أبغى كيلو طلح ونصف كيلو سمر")
    assert len(intents) == 2
    names = {i["product_name"] for i in intents}
    assert any("طلح" in n for n in names)
    assert any("سمر" in n for n in names)


def test_extract_update_quantity() -> None:
    intents = extract_cart_intents("خلي الطلح حبتين")
    assert len(intents) == 1
    assert intents[0]["action"] == "update_quantity"
    assert intents[0]["quantity"] == 2


def test_extract_increment_quantity() -> None:
    intents = extract_cart_intents("زود الطلح واحد")
    assert intents[0]["action"] == "increment_quantity"


def test_extract_update_variant() -> None:
    intents = extract_cart_intents("بدل الطلح كيلو خله نصف كيلو")
    assert len(intents) == 1
    assert intents[0]["action"] == "update_variant"
    assert intents[0]["new_variant"] == "500g"


def test_extract_remove_item() -> None:
    intents = extract_cart_intents("احذف السمر")
    assert intents[0]["action"] == "remove_item"
    assert "سمر" in intents[0]["product_name"]


def test_extract_remove_without_product_is_empty() -> None:
    assert extract_cart_intents("مرحبا") == []


def test_apply_add_builds_cart_items() -> None:
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()
    cart, deltas, changed = apply_cart_intents_to_state(
        state=state,
        prep=prep,
        intents=extract_cart_intents("أبغى كيلو طلح"),
    )
    assert changed is True
    assert len(cart) == 1
    assert "طلح" in cart[0]["product_name"]
    assert len(prep.cart_deltas) == 1
    assert len(state.cart_items) == 1


def test_apply_remove_does_not_break_on_missing() -> None:
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()
    cart, _, changed = apply_cart_intents_to_state(
        state=state,
        prep=prep,
        intents=extract_cart_intents("احذف السدر"),
    )
    assert changed is True
    assert cart == []


def test_second_add_merges_via_state() -> None:
    state = SimpleNamespace(
        cart_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1}],
        current_product_focus={},
    )
    prep = OrderPreparationState(
        line_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1}],
    )
    cart, _, changed = apply_cart_intents_to_state(
        state=state,
        prep=prep,
        intents=extract_cart_intents("أضف كيلو طلح"),
        product_info={"title": "عسل طلح", "external_id": "p1"},
    )
    assert changed
    assert len(cart) == 1
    assert cart[0]["quantity"] == 2


def test_maybe_apply_cart_message_noop_on_greeting() -> None:
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()
    cart, deltas, changed = maybe_apply_cart_message(
        state=state, prep=prep, message="السلام عليكم",
    )
    assert changed is False
    assert cart == []
    assert deltas == []


def test_focus_not_wiped_when_cart_exists() -> None:
    state = SimpleNamespace(
        cart_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1}],
        current_product_focus={"title": "عسل طلح", "id": "p1"},
    )
    prep = OrderPreparationState(
        line_items=[{"product_name": "عسل طلح", "variant": "1kg", "quantity": 1}],
    )
    apply_cart_intents_to_state(
        state=state,
        prep=prep,
        intents=extract_cart_intents("أضف نصف كيلو سمر"),
        product_info={"title": "عسل سمر", "external_id": "p2"},
    )
    assert len(state.cart_items) == 2
    assert state.current_product_focus.get("title") == "عسل سمر"
