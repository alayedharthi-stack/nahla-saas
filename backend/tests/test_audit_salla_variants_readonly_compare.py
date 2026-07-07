"""Unit tests for variant compare helpers (no DB / no Salla API)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audit_salla_variants_vs_nahla_readonly import (  # noqa: E402
    coerce_price,
    compare_product_variants,
    extract_salla_variant_row,
)


def test_coerce_price_from_dict_and_whole_number():
    assert coerce_price({"amount": 59.0}) == "59"
    assert coerce_price(119) == "119"
    assert coerce_price(None) is None


def test_extract_salla_variant_row_related_options():
    raw = {
        "id": 9001,
        "sku": "SKU-RED-M",
        "price": {"amount": 59},
        "sale_price": 59,
        "regular_price": 119,
        "quantity": 3,
        "available": True,
        "related_option_values": [{"name": "اللون", "value": "أحمر"}],
        "image_url": "https://cdn.example/v.jpg",
    }
    row = extract_salla_variant_row(raw)
    assert row["salla_variant_id"] == "9001"
    assert row["price"] == "59"
    assert row["sale_price"] == "59"
    assert row["regular_price"] == "119"
    assert row["stock_quantity"] == 3
    assert row["in_stock"] is True
    assert row["options"]


def test_compare_pass_when_variant_present():
    salla = [extract_salla_variant_row({"id": 1, "price": 50, "quantity": 2, "available": True})]
    nahla = [{
        "salla_variant_id": "1",
        "sku": None,
        "price": "50",
        "sale_price": None,
        "regular_price": None,
        "stock_quantity": 2,
        "in_stock": True,
        "options": None,
        "option_summary": None,
        "image_url": None,
        "retailer_id": "x-1",
        "is_default": False,
    }]
    report = compare_product_variants(salla, nahla)
    assert not report.has_fail
    assert any(f.code == "present_in_nahla" for f in report.findings)


def test_compare_fail_missing_in_nahla():
    salla = [extract_salla_variant_row({"id": 99, "price": 10, "quantity": 1})]
    report = compare_product_variants(salla, [])
    assert report.has_fail
    assert any(f.code == "missing_in_nahla" for f in report.findings)


def test_compare_warn_sale_price_missing_in_nahla():
    salla = [extract_salla_variant_row({
        "id": 2, "price": 40, "sale_price": 40, "regular_price": 80, "quantity": 1,
    })]
    nahla = [{
        "salla_variant_id": "2",
        "price": "40",
        "sale_price": None,
        "regular_price": None,
        "stock_quantity": 1,
        "in_stock": True,
        "options": None,
        "option_summary": None,
        "image_url": None,
        "retailer_id": None,
        "is_default": False,
        "sku": None,
    }]
    report = compare_product_variants(salla, nahla)
    assert not report.has_fail
    assert any(f.code == "sale_price_missing_in_nahla" for f in report.findings)
    assert any(f.code == "regular_price_missing_in_nahla" for f in report.findings)


def test_compare_fail_stale_in_nahla_in_stock():
    nahla = [{
        "salla_variant_id": "77",
        "price": "10",
        "sale_price": None,
        "regular_price": None,
        "stock_quantity": 1,
        "in_stock": True,
        "options": None,
        "option_summary": None,
        "image_url": None,
        "retailer_id": None,
        "is_default": False,
        "sku": None,
    }]
    report = compare_product_variants([], nahla)
    assert report.has_fail
    assert any(f.code == "stale_in_nahla" for f in report.findings)


def test_compare_pass_soft_pruned_nahla_variant():
    nahla = [{
        "salla_variant_id": "88",
        "price": "10",
        "sale_price": None,
        "regular_price": None,
        "stock_quantity": 0,
        "in_stock": False,
        "options": None,
        "option_summary": None,
        "image_url": None,
        "retailer_id": None,
        "is_default": False,
        "sku": None,
    }]
    report = compare_product_variants([], nahla)
    assert not report.has_fail
    assert any(f.code == "nahla_soft_pruned" for f in report.findings)
