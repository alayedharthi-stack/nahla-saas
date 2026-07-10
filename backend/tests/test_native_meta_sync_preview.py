"""Tests for Nahla-native Meta sync dry-run preview (Phase 3B-1)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_NAHLA_MANAGED,
    OWNERSHIP_META_READONLY,
    SOURCE_NAHLA_NATIVE,
    is_meta_export_eligible,
)
from services.meta_catalog_sync_preview import preview_native_meta_sync  # noqa: E402


def _conn(*, catalog_id: str = "CAT-GENERIC-001"):
    return SimpleNamespace(
        tenant_id=9,
        meta_catalog_id=catalog_id,
        provider="meta",
        connection_type="direct",
        access_token="EAAB-test-token",
    )


def _native_parent(
    *,
    pid: int = 101,
    title: str = "حذاء رياضي أبيض",
    source: str = SOURCE_NAHLA_NATIVE,
    ownership_mode: str = OWNERSHIP_NAHLA_MANAGED,
    price: str = "199",
    image_url: str = "https://cdn.example/shoe.jpg",
    product_url: str = "https://store.example/p/shoe",
):
    return SimpleNamespace(
        id=pid,
        tenant_id=9,
        title=title,
        external_id=None,
        description="حذاء رياضي مريح",
        price=price,
        sku=None,
        meta_retailer_id=None,
        in_stock=True,
        stock_quantity=5,
        source=source,
        ownership_mode=ownership_mode,
        extra_metadata={
            "image_url": image_url,
            "product_url": product_url,
            "currency": "SAR",
        },
    )


def _salla_parent():
    return SimpleNamespace(
        id=202,
        tenant_id=9,
        title="عسل سدر",
        external_id="salla-9001",
        description=None,
        price="120",
        sku=None,
        meta_retailer_id=None,
        in_stock=True,
        stock_quantity=1,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        extra_metadata={"image_url": "https://cdn.example/h.jpg", "product_url": "https://x"},
    )


def _meta_existing_parent():
    return SimpleNamespace(
        id=303,
        tenant_id=9,
        title="منتج Meta",
        external_id=None,
        description=None,
        price="50",
        sku=None,
        meta_retailer_id="meta-rid-1",
        in_stock=True,
        stock_quantity=1,
        source="meta",
        ownership_mode=OWNERSHIP_META_READONLY,
        extra_metadata={"image_url": "https://cdn.example/m.jpg", "product_url": "https://x"},
    )


def _variant(parent):
    return SimpleNamespace(
        id=501,
        tenant_id=9,
        product_id=parent.id,
        salla_variant_id=None,
        retailer_id="nahla_p_101",
        price=parent.price,
        currency="SAR",
        stock_quantity=parent.stock_quantity,
        in_stock=True,
        option_summary=None,
        options={},
        image_url=None,
        extra_metadata=parent.extra_metadata,
        is_default=True,
    )


def _mock_db(*, parent=None, variant=None, conn=None):
    db = MagicMock()
    parent = parent or _native_parent()
    variant = variant if variant is not False else None
    conn = conn or _conn()

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            q.filter.return_value.first.return_value = parent
        elif name == "ProductVariant":
            q.filter.return_value.order_by.return_value.first.return_value = variant
        elif name == "WhatsAppConnection":
            q.filter.return_value.first.return_value = conn
        return q

    db.query.side_effect = _query
    return db


def test_is_meta_export_eligible_native_and_manual():
    assert is_meta_export_eligible(_native_parent()) is True
    assert is_meta_export_eligible(_native_parent(source="manual")) is True


def test_is_meta_export_eligible_rejects_external_sources():
    assert is_meta_export_eligible(_salla_parent()) is False
    assert is_meta_export_eligible(_meta_existing_parent()) is False


def test_native_preview_succeeds_without_graph_write():
    parent = _native_parent()
    db = _mock_db(parent=parent, variant=_variant(parent))
    with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
        with patch.object(httpx.Client, "post") as post_mock:
            with patch.object(httpx.Client, "put") as put_mock:
                with patch.object(httpx.Client, "delete") as delete_mock:
                    result = preview_native_meta_sync(db, 9, 101)

    assert result["eligible"] is True
    assert result["dry_run"] is True
    assert result["would_sync"] is False
    assert result["product_id"] == 101
    assert result["meta_catalog_id"] == "CAT-GENERIC-001"
    assert result["payload"]["name"]
    assert result["fatal_errors"] == []
    post_mock.assert_not_called()
    put_mock.assert_not_called()
    delete_mock.assert_not_called()


def test_legacy_manual_product_preview_succeeds():
    parent = _native_parent(source="manual")
    db = _mock_db(parent=parent, variant=_variant(parent))
    with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
        result = preview_native_meta_sync(db, 9, 101)
    assert result["eligible"] is True
    assert result["dry_run"] is True


def test_salla_product_preview_rejected():
    parent = _salla_parent()
    db = _mock_db(parent=parent, variant=False)
    result = preview_native_meta_sync(db, 9, 202)
    assert result["eligible"] is False
    assert result["error_code"] == "product_not_meta_export_eligible"


def test_meta_existing_product_preview_rejected():
    parent = _meta_existing_parent()
    db = _mock_db(parent=parent, variant=False)
    result = preview_native_meta_sync(db, 9, 303)
    assert result["eligible"] is False
    assert result["error_code"] == "product_not_meta_export_eligible"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("image_url", None, "missing_image_url"),
        ("price", None, "missing_price"),
        ("product_url", None, "missing_url"),
    ],
)
def test_missing_required_fields_return_fatal_errors(field, value, code):
    parent = _native_parent()
    variant = _variant(parent)
    if field == "price":
        parent.price = value
        variant.price = value
    elif field == "image_url":
        parent.extra_metadata = dict(parent.extra_metadata)
        parent.extra_metadata["image_url"] = value
        variant.extra_metadata = dict(parent.extra_metadata)
    else:
        parent.extra_metadata = dict(parent.extra_metadata)
        parent.extra_metadata["product_url"] = value
        variant.extra_metadata = dict(parent.extra_metadata)
    db = _mock_db(parent=parent, variant=variant)
    with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
        result = preview_native_meta_sync(db, 9, 101)
    assert result["eligible"] is True
    assert any(item["code"] == code for item in result["fatal_errors"])


def test_missing_catalog_id_returns_catalog_error():
    parent = _native_parent()
    db = _mock_db(parent=parent, variant=_variant(parent), conn=_conn(catalog_id=""))
    result = preview_native_meta_sync(db, 9, 101)
    assert result["eligible"] is False
    assert result["error_code"] == "catalog_id_missing"


def test_native_without_variant_uses_synthetic_preview():
    parent = _native_parent()
    db = _mock_db(parent=parent, variant=None)
    with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
        result = preview_native_meta_sync(db, 9, 101)
    assert result["eligible"] is True
    assert result["payload"]["retailer_id"]
    assert any(w["code"] == "variant_synthesized_for_preview" for w in result["warnings"])


def test_generic_merchant_product_title_in_payload():
    parent = _native_parent(title="حذاء رياضي أبيض")
    db = _mock_db(parent=parent, variant=_variant(parent))
    with patch("services.meta_catalog_push._select_graph_token", return_value={"token": "tok"}):
        result = preview_native_meta_sync(db, 9, 101)
    assert "حذاء رياضي أبيض" in str(result["payload"].get("name") or "")


def test_router_rejects_ineligible_with_409():
    import asyncio

    from fastapi import HTTPException  # noqa: PLC0415

    from routers.catalog import merchant_catalog_meta_sync_preview  # noqa: PLC0415

    parent = _salla_parent()
    db = _mock_db(parent=parent, variant=False)
    request = MagicMock()
    with patch("routers.catalog.resolve_tenant_id", return_value=9):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                merchant_catalog_meta_sync_preview(
                    202, request=request, db=db, _user={"sub": "u1"}
                )
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "product_not_meta_export_eligible"
