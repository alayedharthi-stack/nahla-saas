"""Tests for catalog product image upload (Cloudflare R2 abstraction)."""

from __future__ import annotations

import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services import catalog_media_storage as cms  # noqa: E402


def _png_bytes(width: int = 64, height: int = 64) -> bytes:
    img = Image.new("RGB", (width, height), color=(120, 80, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    img = Image.new("RGB", (48, 48), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    img = Image.new("RGB", (32, 32), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _catalog_media_env(monkeypatch):
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_BUCKET", "nahlah-media")
    monkeypatch.setenv(
        "NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL",
        "https://pub-example.r2.dev",
    )
    monkeypatch.setenv(
        "NAHLA_CATALOG_MEDIA_R2_ENDPOINT",
        "https://account123.r2.cloudflarestorage.com",
    )
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_SECRET_ACCESS_KEY", "test-secret")


def test_sniff_accepts_jpeg_png_webp():
    assert cms.sniff_image_mime(_jpeg_bytes()) == "image/jpeg"
    assert cms.sniff_image_mime(_png_bytes()) == "image/png"
    assert cms.sniff_image_mime(_webp_bytes()) == "image/webp"


def test_sniff_rejects_fake_image_extension():
    assert cms.sniff_image_mime(b"not-an-image-at-all") is None
    assert cms.sniff_image_mime(b"GIF89a") is None


def test_prepare_webp_rejects_oversized_dimensions():
    huge = _png_bytes(width=5000, height=100)
    with pytest.raises(cms.CatalogMediaValidationError, match="image_dimensions_too_large"):
        cms.prepare_catalog_product_webp(huge)


def test_prepare_webp_accepts_valid_png():
    webp, mime = cms.prepare_catalog_product_webp(_png_bytes())
    assert mime == "image/webp"
    assert webp[:4] == b"RIFF"
    assert len(webp) < cms.MAX_UPLOAD_BYTES


def test_tenant_image_url_prefix_and_ownership():
    prefix = cms.tenant_image_url_prefix(42)
    assert prefix == "https://pub-example.r2.dev/catalog-products/42/"
    owned = f"{prefix}abc123.webp"
    assert cms.image_url_owned_by_tenant(42, owned) is True
    assert cms.image_url_owned_by_tenant(42, f"{prefix.replace('/42/', '/99/')}abc.webp") is False
    assert cms.image_url_owned_by_tenant(42, "https://evil.example/x.jpg") is False
    assert cms.image_url_owned_by_tenant(42, None) is True


@patch("services.catalog_media_storage._s3_client")
def test_upload_catalog_product_image_puts_to_r2(mock_client_factory):
    mock_client = MagicMock()
    mock_client_factory.return_value = mock_client

    result = cms.upload_catalog_product_image(tenant_id=7, content=_jpeg_bytes())

    assert result["content_type"] == "image/webp"
    assert result["size_bytes"] > 0
    assert result["media_id"]
    assert result["image_url"].startswith(
        "https://pub-example.r2.dev/catalog-products/7/"
    )
    assert result["image_url"].endswith(".webp")

    mock_client.put_object.assert_called_once()
    kwargs = mock_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "nahlah-media"
    assert kwargs["Key"].startswith("catalog-products/7/")
    assert kwargs["ContentType"] == "image/webp"
    assert kwargs["Metadata"]["tenant-id"] == "7"
    assert kwargs["Metadata"]["status"] == "pending"


def test_public_url_for_object_key_uses_config_base_only(monkeypatch):
    monkeypatch.setenv(
        "NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL",
        "https://media.nahlah.ai",
    )
    url = cms.public_url_for_object_key("catalog-products/1/abc.webp")
    assert url == "https://media.nahlah.ai/catalog-products/1/abc.webp"


def test_object_key_from_public_url_roundtrip():
    url = "https://pub-example.r2.dev/catalog-products/3/deadbeef.webp"
    assert cms.object_key_from_public_url(url) == "catalog-products/3/deadbeef.webp"
    assert cms.object_key_from_public_url("https://other.example/x.webp") is None


@patch("services.catalog_media_storage._s3_client")
def test_attach_catalog_product_image_marks_attached(mock_client_factory):
    mock_client = MagicMock()
    mock_client_factory.return_value = mock_client
    mock_client.head_object.return_value = {
        "Metadata": {"status": "pending", "tenant-id": "7"},
    }
    url = "https://pub-example.r2.dev/catalog-products/7/abc123.webp"

    cms.attach_catalog_product_image(tenant_id=7, image_url=url, product_id=99)

    mock_client.copy_object.assert_called_once()
    meta = mock_client.copy_object.call_args.kwargs["Metadata"]
    assert meta["status"] == "attached"
    assert meta["product-id"] == "99"
    assert meta["tenant-id"] == "7"


@patch("services.catalog_media_storage._s3_client")
def test_attach_rejects_image_bound_to_other_product(mock_client_factory):
    mock_client = MagicMock()
    mock_client_factory.return_value = mock_client
    mock_client.head_object.return_value = {
        "Metadata": {"status": "attached", "product-id": "1"},
    }
    url = "https://pub-example.r2.dev/catalog-products/7/abc123.webp"

    with pytest.raises(cms.CatalogMediaValidationError, match="image_already_attached"):
        cms.attach_catalog_product_image(tenant_id=7, image_url=url, product_id=2)


@patch("services.catalog_media_storage._s3_client")
def test_attach_idempotent_for_same_product(mock_client_factory):
    mock_client = MagicMock()
    mock_client_factory.return_value = mock_client
    mock_client.head_object.return_value = {
        "Metadata": {"status": "attached", "product-id": "99"},
    }
    url = "https://pub-example.r2.dev/catalog-products/7/abc123.webp"

    cms.attach_catalog_product_image(tenant_id=7, image_url=url, product_id=99)

    mock_client.copy_object.assert_called_once()
