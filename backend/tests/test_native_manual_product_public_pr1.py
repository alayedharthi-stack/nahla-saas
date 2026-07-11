"""PR1 tests: native manual product public URL, variant, readiness."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_NAHLA_NATIVE,
)
from core.native_product_public_url import (  # noqa: E402
    build_native_product_public_url,
    is_valid_https_product_url,
    resolve_product_public_url,
)
from routers.public_catalog import (  # noqa: E402
    _load_public_native_product,
    plain_description,
)
from services.meta_catalog_export import (  # noqa: E402
    meta_price_minor_units,
    preview_meta_variant_payload,
)
from core.native_product_public_url import resolve_meta_export_product_url  # noqa: E402
from services.meta_catalog_sync_confirm import ensure_native_default_variant  # noqa: E402
from services.product_publication_status import build_product_publication_status  # noqa: E402


def _native_parent(
    *,
    pid: int = 176,
    tenant_id: int = 9,
    title: str = "عطر ورد 100ml",
    price: str = "1",
    product_url: str | None = None,
    with_image: bool = True,
):
    meta = {"currency": "SAR", "source": SOURCE_NAHLA_NATIVE}
    if with_image:
        meta["image_url"] = "https://pub.example/catalog-products/9/abc.webp"
    if product_url:
        meta["product_url"] = product_url
    return SimpleNamespace(
        id=pid,
        tenant_id=tenant_id,
        title=title,
        description="وصف قصير",
        price=price,
        sku=None,
        meta_retailer_id=f"nahla_p_{pid}",
        canonical_retailer_id=f"nahla_p_{pid}",
        in_stock=True,
        stock_quantity=5,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        extra_metadata=meta,
        catalog_status="active",
        merchant_hidden_at=None,
        sync_status=None,
    )


@patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False)
def test_build_native_product_public_url_uses_api_base() -> None:
    url = build_native_product_public_url("nahla_p_176")
    assert url == "https://api.nahlah.ai/public/catalog/items/nahla_p_176"


def test_is_valid_https_product_url() -> None:
    assert is_valid_https_product_url("https://store.example/p/shoe")
    assert not is_valid_https_product_url("http://store.example/p/shoe")
    assert not is_valid_https_product_url("javascript:alert(1)")


@patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False)
def test_resolve_product_url_priority_merchant_https() -> None:
    parent = _native_parent(product_url=None)
    merchant = "https://merchant.example/p/1"
    assert resolve_product_public_url(parent, merchant_product_url=merchant) == merchant


@patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False)
def test_resolve_product_url_falls_back_to_nahla_public() -> None:
    parent = _native_parent(product_url=None)
    url = resolve_product_public_url(parent)
    assert url == "https://api.nahlah.ai/public/catalog/items/nahla_p_176"


@patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False)
def test_invalid_merchant_http_url_falls_back_to_public() -> None:
    parent = _native_parent(product_url=None)
    url = resolve_product_public_url(parent, merchant_product_url="http://insecure.example/p")
    assert url == "https://api.nahlah.ai/public/catalog/items/nahla_p_176"


def test_one_sar_serializes_to_100_minor_units() -> None:
    assert meta_price_minor_units("1") == 100
    parent = _native_parent(price="1")
    variant = SimpleNamespace(
        id=501,
        price="1",
        currency="SAR",
        in_stock=True,
        stock_quantity=1,
        retailer_id="nahla_p_176",
        options={},
        image_url=parent.extra_metadata["image_url"],
        extra_metadata=parent.extra_metadata,
    )
    with patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False):
        report = preview_meta_variant_payload(parent, variant)
    assert report["payload"]["price"] == 100
    assert report["payload"]["currency"] == "SAR"
    assert "missing_url" not in report["warnings"]


@patch.dict(os.environ, {"NAHLA_PUBLIC_API_BASE_URL": "https://api.nahlah.ai"}, clear=False)
def test_meta_export_url_fallback_when_metadata_empty() -> None:
    parent = _native_parent(product_url=None)
    variant = SimpleNamespace(
        retailer_id="nahla_p_176",
        extra_metadata=parent.extra_metadata,
    )
    assert resolve_meta_export_product_url(parent, variant) == (
        "https://api.nahlah.ai/public/catalog/items/nahla_p_176"
    )


def test_ensure_default_variant_idempotent() -> None:
    parent = _native_parent()
    db = MagicMock()
    existing = SimpleNamespace(id=900, retailer_id="nahla_p_176", is_default=True)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = existing
    variant, created = ensure_native_default_variant(db, parent)
    assert variant is existing
    assert created is False
    db.add.assert_not_called()


def test_publication_status_does_not_claim_visible() -> None:
  status = build_product_publication_status(_native_parent())
  assert status["visible_in_whatsapp"] is False
  assert status["waba_catalog_linked"] is None
  assert status["meta_catalog_synced"] is False


def test_plain_description_escapes_html() -> None:
    assert plain_description("Tom & Jerry <b>x</b>") == "Tom &amp; Jerry x"
    assert "<b>" not in plain_description("Tom & Jerry <b>x</b>")


def test_load_public_native_product_requires_single_match() -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    assert _load_public_native_product(db, "nahla_p_176") is None

    p1 = SimpleNamespace(
        id=1,
        tenant_id=9,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        merchant_hidden_at=None,
        catalog_status="active",
    )
    p2 = SimpleNamespace(
        id=2,
        tenant_id=10,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        merchant_hidden_at=None,
        catalog_status="active",
    )
    db.query.return_value.filter.return_value.all.return_value = [p1, p2]
    assert _load_public_native_product(db, "nahla_p_176") is None


def test_public_route_renders_without_internal_ids() -> None:
    from main import app  # noqa: PLC0415

    parent = _native_parent(title="قميص قطني أزرق")
    tenant = SimpleNamespace(id=9, name="متجر تجريبي عام", is_active=True)
    with patch("routers.public_catalog._load_public_native_product", return_value=(parent, tenant)):
        with patch("routers.public_catalog._whatsapp_cta_url", return_value="https://wa.me/966500000000"):
            client = TestClient(app)
            resp = client.get("/public/catalog/items/nahla_p_176")
    assert resp.status_code == 200
    assert "قميص قطني أزرق" in resp.text
    assert "tenant_id=9" not in resp.text


def test_public_route_404_for_missing() -> None:
    from main import app  # noqa: PLC0415

    with patch("routers.public_catalog._load_public_native_product", return_value=None):
        client = TestClient(app)
        resp = client.get("/public/catalog/items/nahla_p_missing")
    assert resp.status_code == 404
    assert "المنتج غير متاح" in resp.text
