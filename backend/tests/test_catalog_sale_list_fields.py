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
    assert fields["sale_price"] == "59.0"
    assert fields["regular_price"] == "119.0"
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
