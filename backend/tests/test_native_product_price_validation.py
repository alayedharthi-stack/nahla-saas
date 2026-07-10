"""Tests for Nahla-native product price validation (PR A)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    normalize_catalog_price_amount,
    validate_native_product_price,
)
from fastapi import HTTPException  # noqa: E402
from routers.catalog import _coerce_native_price_or_422  # noqa: E402
from services.meta_catalog_export import (  # noqa: E402
    meta_price_minor_units,
    preview_meta_variant_payload,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("199", "199"),
        ("199.5", "199.5"),
        ("59.50", "59.5"),
        (199, "199"),
    ],
)
def test_normalize_accepts_numeric_values(raw, expected) -> None:
    assert normalize_catalog_price_amount(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "199 SAR",
        "SAR 199",
        "199 ريال",
        "١٩٩",
        "not-a-price",
        "-10",
    ],
)
def test_validate_rejects_non_numeric_native_price(raw) -> None:
    with pytest.raises(ValueError, match="price_must_be_numeric"):
        validate_native_product_price(raw)


def test_coerce_native_price_or_422_returns_structured_detail() -> None:
    with pytest.raises(HTTPException) as exc:
        _coerce_native_price_or_422("199 SAR")
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "price_must_be_numeric"
    assert "199" in exc.value.detail["message_ar"]


def test_legacy_bad_price_still_fatal_in_preview() -> None:
    parent = SimpleNamespace(
        id=101,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        description="وصف",
        price="199 SAR",
        extra_metadata={
            "image_url": "https://cdn.example/shoe.jpg",
            "product_url": "https://store.example/shoe",
            "currency": "SAR",
        },
        in_stock=True,
    )
    variant = SimpleNamespace(
        id=None,
        price=parent.price,
        currency="SAR",
        in_stock=True,
        stock_quantity=1,
        retailer_id="nahla_p_101",
        options={},
        extra_metadata=parent.extra_metadata,
    )
    report = preview_meta_variant_payload(parent, variant)
    assert "missing_price" in report["warnings"]
    assert report["fatal"] is True


def test_preview_with_numeric_price_has_minor_units() -> None:
    parent = SimpleNamespace(
        id=102,
        tenant_id=9,
        title="حذاء رياضي أبيض",
        description="وصف",
        price="199",
        extra_metadata={
            "image_url": "https://cdn.example/shoe.jpg",
            "product_url": "https://store.example/shoe",
            "currency": "SAR",
        },
        in_stock=True,
    )
    variant = SimpleNamespace(
        id=None,
        price="199",
        currency="SAR",
        in_stock=True,
        stock_quantity=1,
        retailer_id="nahla_p_102",
        options={},
        extra_metadata=parent.extra_metadata,
    )
    report = preview_meta_variant_payload(parent, variant)
    assert report["payload"]["price"] == 19900
    assert "missing_price" not in report["warnings"]


def test_meta_price_minor_units_numeric_string() -> None:
    assert meta_price_minor_units("199") == 19900


def test_create_native_price_validation_via_coerce() -> None:
    assert _coerce_native_price_or_422("199") == "199"
    assert _coerce_native_price_or_422("59.5") == "59.5"
    assert _coerce_native_price_or_422(None) is None
    assert _coerce_native_price_or_422("") is None
