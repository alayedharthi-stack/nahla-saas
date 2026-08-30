"""Localized Meta Graph price read-back for catalog content verification.

Live Production fixture (tenant 1 / product 177, Graph GET after a successful
create): price displayed as Arabic-Indic major units with a riyal marker,
currency field independently ``SAR``, expected minor units ``100``.
"""
from __future__ import annotations

import inspect
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.native_meta_sync_orchestrator import (  # noqa: E402
    LOOKUP_UNVERIFIED_FIELDS,
    LOOKUP_VERIFIED_FIELDS,
    _price_minor,
    compare_pushed_content_to_lookup,
)

# Sanitized copy of the live Graph ``price`` string. No tokens, no catalog IDs.
LIVE_GRAPH_PRICE_AR_RIYAL = "١٫٠٠ ر.س"
EXPECTED_MINOR = 100
EXPECTED_CURRENCY = "SAR"


def _payload(**overrides):
    base = {
        "price": EXPECTED_MINOR,
        "currency": EXPECTED_CURRENCY,
        "availability": "in stock",
    }
    base.update(overrides)
    return base


def test_live_arabic_riyal_price_compares_to_expected_minor_units():
    result = compare_pushed_content_to_lookup(
        _payload(),
        {
            "id": "META-CANARY",
            "retailer_id": "sku-generic-perfume",
            "name": "عطر ورد 100ml",
            "price": LIVE_GRAPH_PRICE_AR_RIYAL,
            "currency": EXPECTED_CURRENCY,
            "availability": "in stock",
        },
    )
    assert "price" not in result["missing_fields"]
    assert result["missing_fields"] == []
    assert result["mismatched_fields"] == []
    assert result["outcome"] == "matched"
    assert _price_minor(LIVE_GRAPH_PRICE_AR_RIYAL) == EXPECTED_MINOR


def test_price_minor_arabic_and_latin_display_strings():
    assert _price_minor("١٫٠٠ ر.س") == 100
    assert _price_minor("۱٫۰۰ SAR") == 100
    assert _price_minor("١٬٢٣٤٫٥٦ ر.س") == 123456
    assert _price_minor("1.00 SAR") == 100
    assert _price_minor("SAR 1.00") == 100
    assert _price_minor("ر.س.‏ 1.00") == 100


def test_raw_integer_minor_units_contract_unchanged():
    assert _price_minor(100) == 100
    assert _price_minor(14900) == 14900
    assert _price_minor("100") == 100


def test_different_price_does_not_match():
    result = compare_pushed_content_to_lookup(
        _payload(price=100),
        {"price": "٢٫٠٠ ر.س", "currency": "SAR", "availability": "in stock"},
    )
    assert result["outcome"] == "mismatch"
    assert "price" in result["mismatched_fields"]


def test_different_currency_does_not_match():
    result = compare_pushed_content_to_lookup(
        _payload(),
        {
            "price": LIVE_GRAPH_PRICE_AR_RIYAL,
            "currency": "USD",
            "availability": "in stock",
        },
    )
    assert result["outcome"] == "mismatch"
    assert "currency" in result["mismatched_fields"]
    assert "price" not in result["mismatched_fields"]
    assert "price" not in result["missing_fields"]


def test_empty_corrupt_or_ambiguous_price_is_missing():
    for value in (None, "", "   ", "abc", "1.00 2.00", "١٫٠٠ ٢٫٠٠", True, 1.0):
        assert _price_minor(value) is None
    incomplete = compare_pushed_content_to_lookup(
        _payload(),
        {"price": "not-a-price", "currency": "SAR", "availability": "in stock"},
    )
    assert incomplete["outcome"] == "incomplete"
    assert incomplete["missing_fields"] == ["price"]


def test_price_parser_does_not_use_float():
    source = inspect.getsource(_price_minor)
    assert "float(" not in source
    assert "float " not in source


def test_identity_and_unverified_fields_remain_outside_content_compare():
    assert "retailer_id" in LOOKUP_VERIFIED_FIELDS
    assert "name" in LOOKUP_VERIFIED_FIELDS
    assert "image_url" in LOOKUP_UNVERIFIED_FIELDS
    result = compare_pushed_content_to_lookup(
        _payload(),
        {
            "id": "META-CANARY",
            "retailer_id": "sku-generic-perfume",
            "name": "عطر ورد 100ml",
            "price": LIVE_GRAPH_PRICE_AR_RIYAL,
            "currency": EXPECTED_CURRENCY,
            "availability": "in stock",
            "url": "https://example.test/p/sku-generic-perfume",
            "image_url": "https://cdn.example/sku-generic-perfume.webp",
        },
    )
    assert result["outcome"] == "matched"
    assert set(result["checked_fields"]) == {"price", "currency", "availability"}
