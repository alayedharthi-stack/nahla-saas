"""Salla sale display: regular_price as the 'before' reference."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.store_knowledge import format_product_price_str  # noqa: E402
from services.store_sync import _normalise_product  # noqa: E402


def test_format_sale_price_uses_regular_price_when_equal_to_price():
    """Salla on-sale shape: price=sale_price=74, regular_price=149."""
    text = format_product_price_str(
        price="74",
        sale_price="74",
        regular_price="149",
    )
    assert text == "74 ريال (بدلاً من 149 ريال)"
    assert "بدلاً من 74" not in text


def test_format_sale_price_strips_trailing_zero_from_synced_strings():
    """Post-sync metadata often stores prices as '74.0' / '149.0'."""
    text = format_product_price_str(
        price="74.0",
        sale_price="74.0",
        regular_price="149.0",
    )
    assert text == "74 ريال (بدلاً من 149 ريال)"


def test_format_sale_price_keeps_fractional_amounts():
    text = format_product_price_str(
        price="74.5",
        sale_price="74.5",
        regular_price="149",
    )
    assert text == "74.5 ريال (بدلاً من 149 ريال)"


def test_format_sale_price_without_distinct_regular_omits_before_clause():
    text = format_product_price_str(price="99", sale_price="99", regular_price=None)
    assert text == "99 ريال"
    assert "بدلاً من" not in text

    text_equal = format_product_price_str(
        price="74.0",
        sale_price="74.0",
        regular_price="74.0",
    )
    assert text_equal == "74 ريال"
    assert "بدلاً من" not in text_equal


def test_normalise_product_persists_regular_price_in_metadata():
    synced = _normalise_product(
        {
            "id": "1455849494",
            "title": "عطر ورد 100ml",
            "price": 74,
            "sale_price": 74,
            "regular_price": 149,
        }
    )
    assert synced["sale_price"] == "74"
    assert synced["regular_price"] == "149"
