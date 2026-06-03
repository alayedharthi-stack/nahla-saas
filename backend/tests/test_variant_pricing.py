"""Generic variant-aware pricing — unit binding and budget math."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from modules.ai.brain.commerce.variant_pricing import (  # noqa: E402
    UnitKind,
    bindings_from_catalog_product,
    calculate_quantity_for_budget,
    evaluate_variant_pricing_turn,
    parse_unit_from_text,
    resolve_variant,
)


def _product_with_variants() -> dict:
    return {
        "id": 101,
        "title": "Premium Widget",
        "variants": [
            {
                "id": 1,
                "option_summary": "250g",
                "price": "126",
                "in_stock": True,
            },
            {
                "id": 2,
                "option_summary": "500g",
                "price": "193",
                "in_stock": True,
            },
            {
                "id": 3,
                "option_summary": "1kg",
                "price": "387",
                "in_stock": True,
            },
            {
                "id": 4,
                "option_summary": "5kg",
                "price": "1475",
                "in_stock": True,
            },
        ],
    }


def test_parse_unit_weight_and_size() -> None:
    u = parse_unit_from_text("250g pack")
    assert u is not None
    assert u.kind == UnitKind.WEIGHT
    assert u.magnitude == pytest.approx(0.25)

    u_kg = parse_unit_from_text("1kg")
    assert u_kg is not None
    assert u_kg.magnitude == pytest.approx(1.0)

    u_size = parse_unit_from_text("Large size")
    assert u_size is not None
    assert u_size.kind == UnitKind.SIZE


def test_bindings_from_catalog_product() -> None:
    bindings = bindings_from_catalog_product(_product_with_variants())
    assert len(bindings) == 4
    by_label = {b.variant_label: b for b in bindings}
    assert by_label["250g"].price == 126.0
    assert by_label["1kg"].price == 387.0
    assert by_label["250g"].unit.normalized_key != by_label["1kg"].unit.normalized_key


def test_price_quote_without_size_is_ambiguous() -> None:
    bindings = bindings_from_catalog_product(_product_with_variants())
    outcome = evaluate_variant_pricing_turn(
        "what is the price?",
        product=_product_with_variants(),
        tenant_id=1,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "clarify"
    assert "250g" in outcome["question"] or "1kg" in outcome["question"]
    assert outcome["root_cause_class"] == "A"


def test_price_quote_with_explicit_unit_uses_matching_variant() -> None:
    outcome = evaluate_variant_pricing_turn(
        "price for 1kg",
        product=_product_with_variants(),
        tenant_id=1,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "reply"
    assert outcome["variant_binding"]["variant_label"] == "1kg"
    assert outcome["variant_binding"]["price"] == 387.0
    assert "387" in outcome["reply_text"]


def test_budget_quantity_uses_1kg_not_250g(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nahla.brain.variant_pricing")
    outcome = evaluate_variant_pricing_turn(
        "600 riyal how many kg can I get?",
        product=_product_with_variants(),
        tenant_id=7,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "reply"
    binding = outcome["variant_binding"]
    assert binding["variant_label"] == "1kg"
    assert binding["price"] == 387.0
    # 600 / 387 ≈ 1.55 kg
    qty_trace = outcome["quantity_trace"]
    assert qty_trace["quantity"] == pytest.approx(600 / 387, rel=0.01)
    logs = caplog.text
    assert "[QUANTITY_CALCULATION_TRACE]" in logs
    assert "[PRICE_RESOLUTION_TRACE]" in logs


def test_budget_kg_without_1kg_variant_clarifies() -> None:
    product = {
        "id": 2,
        "title": "Snack Box",
        "variants": [
            {"id": 10, "option_summary": "250g", "price": "50", "in_stock": True},
        ],
    }
    outcome = evaluate_variant_pricing_turn(
        "600 SAR how many kg?",
        product=product,
        tenant_id=1,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "clarify"
    assert outcome["root_cause_class"] in {"A", "C"}


def test_calculate_quantity_respects_bound_variant() -> None:
    bindings = bindings_from_catalog_product(_product_with_variants())
    bound = next(b for b in bindings if b.variant_label == "500g")
    outcome = calculate_quantity_for_budget(
        400.0,
        variants=bindings,
        bound_variant=bound,
        tenant_id=1,
    )
    assert outcome.status == "resolved"
    assert outcome.variant is not None
    assert outcome.variant.variant_label == "500g"
    assert outcome.quantity == pytest.approx(400 / 193, rel=0.01)


def test_resolve_variant_single_sellable_auto_picks() -> None:
    product = {
        "id": 3,
        "title": "Single SKU Item",
        "variants": [
            {"id": 99, "option_summary": "Pack of 6", "price": "120", "in_stock": True},
        ],
    }
    bindings = bindings_from_catalog_product(product)
    resolved = resolve_variant("how much?", variants=bindings, tenant_id=1)
    assert resolved.status == "resolved"
    assert resolved.variant is not None
    assert resolved.variant.unit.kind == UnitKind.PACK


def test_budget_to_kg_regression_small_variant_price_not_used_as_per_kg() -> None:
    """
    Generic reproduction of the variant-binding failure:
    - Variant A: 250g @ 119 (discounted small pack)
    - Variant B: 1kg @ 387
    - Budget 600 with kg intent must use 1kg basis (~1.55 kg), not 250g (~5 kg).
    """
    product = {
        "id": 501,
        "title": "Bulk Item Alpha",
        "variants": [
            {"id": 1, "option_summary": "250g", "price": "119", "in_stock": True},
            {"id": 2, "option_summary": "1kg", "price": "387", "in_stock": True},
        ],
    }
    outcome = evaluate_variant_pricing_turn(
        "600 riyal how many kg can I get?",
        product=product,
        tenant_id=9,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "reply"
    binding = outcome["variant_binding"]
    assert binding["variant_label"] == "1kg"
    assert binding["price"] == 387.0
    qty_kg = outcome["quantity_trace"]["quantity"]
    assert qty_kg == pytest.approx(600 / 387, rel=0.01)
    assert qty_kg == pytest.approx(1.55, abs=0.05)
    wrong_qty_if_used_250g_price = 600 / 119
    assert qty_kg != pytest.approx(wrong_qty_if_used_250g_price, rel=0.05)
    assert wrong_qty_if_used_250g_price == pytest.approx(5.04, rel=0.02)


def test_arabic_budget_kg_uses_kg_variant_not_smallest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nahla.brain.variant_pricing")
    outcome = evaluate_variant_pricing_turn(
        "أعطاني 600 ريال كم كيلo تجيب",
        product=_product_with_variants(),
        tenant_id=7,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "reply"
    assert outcome["variant_binding"]["variant_label"] == "1kg"
    assert outcome["variant_binding"]["price"] == 387.0
    assert outcome["quantity_trace"]["quantity"] == pytest.approx(600 / 387, rel=0.01)


def test_arabic_price_without_size_clarifies() -> None:
    outcome = evaluate_variant_pricing_turn(
        "كم السعر حاليا",
        product=_product_with_variants(),
        tenant_id=7,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "clarify"


def test_generic_size_product_price_quote() -> None:
    product = {
        "id": 4,
        "title": "T-Shirt",
        "variants": [
            {"id": 1, "option_summary": "Small", "price": "49", "in_stock": True},
            {"id": 2, "option_summary": "Medium", "price": "49", "in_stock": True},
            {"id": 3, "option_summary": "Large", "price": "59", "in_stock": True},
        ],
    }
    outcome = evaluate_variant_pricing_turn(
        "price for Large",
        product=product,
        tenant_id=1,
    )
    assert outcome is not None
    assert outcome["action_kind"] == "reply"
    assert outcome["variant_binding"]["variant_label"] == "Large"
    assert outcome["variant_binding"]["price"] == 59.0
