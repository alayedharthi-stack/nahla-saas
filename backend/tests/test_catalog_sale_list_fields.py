"""Catalog list sale fields — platform-wide Salla offer display flags."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from routers.catalog import _catalog_list_sale_fields  # noqa: E402


def test_on_sale_when_sale_and_regular_differ():
    fields = _catalog_list_sale_fields(
        {"sale_price": "59.0", "regular_price": "119.0"},
    )
    assert fields["sale_price"] == "59"
    assert fields["regular_price"] == "119"
    assert fields["is_on_sale"] is True


def test_not_on_sale_when_prices_equal():
    fields = _catalog_list_sale_fields(
        {"sale_price": "99", "regular_price": "99"},
    )
    assert fields["is_on_sale"] is False


def test_not_on_sale_when_sale_price_missing_or_zero():
    assert _catalog_list_sale_fields({})["is_on_sale"] is False
    assert _catalog_list_sale_fields({"sale_price": "0", "regular_price": "119"})["is_on_sale"] is False


def test_generic_merchant_product_not_blouse_specific():
    """Any product metadata shape — not tied to a tenant or product name."""
    fields = _catalog_list_sale_fields(
        {
            "sale_price": "120",
            "regular_price": "150",
            "title": "حذاء رياضي أبيض",
        },
    )
    assert fields["is_on_sale"] is True


def test_dict_price_normalizes_to_amount_string():
    from routers.catalog import _normalize_catalog_price_amount  # noqa: E402

    assert _normalize_catalog_price_amount({"amount": 59, "currency": "SAR"}) == "59"
    assert _normalize_catalog_price_amount({"amount": 59.5, "currency": "SAR"}) == "59.5"


def test_dict_sale_fields_produce_on_sale_when_amounts_differ():
    fields = _catalog_list_sale_fields(
        {
            "sale_price": {"amount": 59, "currency": "SAR"},
            "regular_price": {"amount": 119, "currency": "SAR"},
        },
    )
    assert fields["sale_price"] == "59"
    assert fields["regular_price"] == "119"
    assert fields["is_on_sale"] is True


def test_zero_dict_amount_not_on_sale():
    fields = _catalog_list_sale_fields(
        {
            "sale_price": {"amount": 0, "currency": "SAR"},
            "regular_price": {"amount": 119, "currency": "SAR"},
        },
    )
    assert fields["sale_price"] is None
    assert fields["is_on_sale"] is False


def test_python_dict_repr_string_returns_none_not_raw():
    from routers.catalog import _normalize_catalog_price_amount  # noqa: E402

    raw = "{'amount': 0, 'currency': 'SAR'}"
    assert _normalize_catalog_price_amount(raw) is None
    fields = _catalog_list_sale_fields(
        {"sale_price": raw, "regular_price": "119"},
    )
    assert fields["sale_price"] is None
    assert fields["is_on_sale"] is False


def test_non_numeric_price_returns_none():
    from routers.catalog import _normalize_catalog_price_amount  # noqa: E402

    assert _normalize_catalog_price_amount("not-a-price") is None
    assert _normalize_catalog_price_amount("[object Object]") is None
