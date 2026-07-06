"""Salla adapter: sale_price / regular_price extraction for catalog display."""

from __future__ import annotations

from store_adapters.salla_adapter import SallaAdapter
from services.store_sync import _normalise_product


def _adapter() -> SallaAdapter:
    return SallaAdapter(api_key="test", store_id="E-demo")


def test_normalize_product_extracts_sale_and_regular_prices():
    """Generic on-sale product: amounts pass through adapter and store_sync."""
    raw = {
        "id": 9001,
        "name": "حذاء رياضي أبيض",
        "price": {"amount": "150", "currency": "SAR"},
        "sale_price": {"amount": "120", "currency": "SAR"},
        "regular_price": {"amount": "150", "currency": "SAR"},
        "quantity": 5,
    }

    product = _adapter()._normalize_product(raw)

    assert product.price == 150.0
    assert product.sale_price == 120.0
    assert product.regular_price == 150.0

    synced = _normalise_product(product)
    assert synced["sale_price"] == "120.0"
    assert synced["price"] == "150.0"


def test_normalize_product_without_sale_price_leaves_field_empty():
    """No sale in payload — do not invent sale_price."""
    raw = {
        "id": 9002,
        "name": "قميص قطني أزرق",
        "price": {"amount": 99},
        "quantity": 3,
    }

    product = _adapter()._normalize_product(raw)

    assert product.price == 99.0
    assert product.sale_price is None
    assert product.regular_price is None

    synced = _normalise_product(product)
    assert synced["sale_price"] == ""
