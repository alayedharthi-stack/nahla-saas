"""Unit tests for generic / ungrounded line-item guard."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.generic_line_item_guard import (  # noqa: E402
    evaluate_line_item_grounding,
    evaluate_order_prep_line_item_grounding,
    is_generic_placeholder_product_name,
    is_grounded_line_item,
    line_items_contain_only_generic_placeholders,
)


@pytest.mark.parametrize(
    "name",
    [
        "منتج",
        "product",
        "item",
        "شيء",
        "غير محدد",
        "المطلوب",
        "صنف",
        "سلعة",
        "PRODUCT",
        "  منتج  ",
    ],
)
def test_generic_placeholder_names_detected(name: str) -> None:
    assert is_generic_placeholder_product_name(name)


@pytest.mark.parametrize(
    "name",
    ["حذاء رياضي أبيض", "عطر ورد 100ml", "sku-real-001"],
)
def test_grounded_names_not_placeholders(name: str) -> None:
    assert not is_generic_placeholder_product_name(name)


def test_generic_only_cart_blocked() -> None:
    items = [{"product_name": "منتج", "quantity": 2}]
    assert line_items_contain_only_generic_placeholders(items)
    decision = evaluate_line_item_grounding(items)
    assert decision.allowed is False
    assert decision.reason == "generic_ungrounded_line_items"


def test_grounded_catalog_item_allowed() -> None:
    items = [
        {
            "product_id": "sku-shoe-001",
            "product_name": "حذاء رياضي أبيض",
            "quantity": 1,
            "catalog_price": 199.0,
        }
    ]
    decision = evaluate_line_item_grounding(items)
    assert decision.allowed is True
    assert decision.reason == "grounded"


def test_mixed_generic_and_grounded_blocked() -> None:
    items = [
        {"product_name": "حذاء رياضي أبيض", "quantity": 1},
        {"product_name": "منتج", "quantity": 1},
    ]
    decision = evaluate_line_item_grounding(items)
    assert decision.allowed is False
    assert decision.reason == "mixed_generic_line_items"


def test_stale_prep_generic_only_blocked() -> None:
    prep = {
        "order_flow_v2_active": True,
        "line_items": [{"product_name": "منتج", "quantity": 1}],
    }
    decision = evaluate_order_prep_line_item_grounding(prep, {})
    assert decision.allowed is False


def test_grounded_line_item_by_product_id_when_name_missing() -> None:
    assert is_grounded_line_item({"product_id": "catalog-sku-42", "quantity": 1})


def test_empty_line_items_allowed_at_guard() -> None:
    decision = evaluate_line_item_grounding([])
    assert decision.allowed is True
    assert decision.reason == "empty"
