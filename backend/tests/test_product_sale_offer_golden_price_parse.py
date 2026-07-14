"""Golden price parsing parity for product_sale_offer SQL/Python alignment."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.product_sale_offer_loader import (  # noqa: E402
    is_strict_product_sale,
)
from modules.ai.brain.truth_surface.product_sale_offer_price_parse import (  # noqa: E402
    canonical_price_string,
    extract_price_raw_from_json_value,
    normalize_extracted_price_raw,
    strict_sale_from_metadata,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,200", "1200"),
        ("1,200.50", "1200.5"),
        ("  80  ", "80"),
        ("80.00", "80"),
        ("59", "59"),
    ],
)
def test_canonical_price_string_accepts_commas_and_whitespace(
    raw: str,
    expected: str,
) -> None:
    assert canonical_price_string(raw) == expected
    assert normalize_extracted_price_raw(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", None, "abc", "-5", "0", "0.0"],
)
def test_canonical_price_string_rejects_invalid(raw) -> None:
    assert canonical_price_string(raw) is None


def test_scalar_json_number_via_metadata() -> None:
    meta = {"sale_price": 59, "regular_price": 79}
    sale, regular, on_sale = strict_sale_from_metadata(meta)
    assert sale == "59"
    assert regular == "79"
    assert on_sale is True
    assert is_strict_product_sale(meta)


def test_object_amount_metadata() -> None:
    meta = {"sale_price": {"amount": "80"}, "regular_price": {"amount": "100"}}
    assert is_strict_product_sale(meta) is True
    assert canonical_price_string({"amount": "80"}) == "80"


def test_nested_amount_object_excluded() -> None:
    meta = {
        "sale_price": {"amount": {"value": "80"}},
        "regular_price": {"amount": "100"},
    }
    assert extract_price_raw_from_json_value(meta["sale_price"]) is None
    assert is_strict_product_sale(meta) is False


def test_object_without_scalar_amount_excluded() -> None:
    meta = {
        "sale_price": {"currency": "SAR"},
        "regular_price": {"amount": "100"},
    }
    assert is_strict_product_sale(meta) is False


@pytest.mark.parametrize(
    "meta",
    [
        {"sale_price": "100", "regular_price": "100"},
        {"sale_price": "120", "regular_price": "100"},
        {"sale_price": "0", "regular_price": "50"},
        {"sale_price": "50", "regular_price": "0"},
        {"sale_price": "-1", "regular_price": "50"},
        {},
    ],
)
def test_strict_sale_matrix_excludes_invalid(meta: dict) -> None:
    assert is_strict_product_sale(meta) is False


def test_strict_sale_valid_generic_merchant() -> None:
    meta = {
        "sale_price": "199",
        "regular_price": "249",
    }
    assert is_strict_product_sale(meta) is True


def test_loader_is_strict_product_sale_uses_price_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.strict_sale_from_metadata",
        lambda _meta: ("1", "2", True),
    )
    assert is_strict_product_sale({}) is True
