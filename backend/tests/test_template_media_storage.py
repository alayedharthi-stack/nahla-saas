"""Tests for durable merchant template-header image storage."""

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
from services import template_media_storage as tms  # noqa: E402


def _png_bytes() -> bytes:
    image = Image.new("RGB", (96, 48), color=(25, 90, 45))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _media_env(monkeypatch):
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_BUCKET", "nahlah-media")
    monkeypatch.setenv(
        "NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL",
        "https://media.example",
    )
    monkeypatch.setenv(
        "NAHLA_CATALOG_MEDIA_R2_ENDPOINT",
        "https://account.r2.cloudflarestorage.com",
    )
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_SECRET_ACCESS_KEY", "test-secret")


@patch("services.template_media_storage._s3_client")
def test_upload_template_header_is_tenant_scoped_and_attached(mock_client_factory):
    client = MagicMock()
    mock_client_factory.return_value = client

    result = tms.upload_template_header_image(tenant_id=7, content=_png_bytes())

    assert result["image_url"].startswith(
        "https://media.example/template-headers/7/"
    )
    assert result["image_url"].endswith(".webp")
    assert result["content_type"] == "image/webp"
    assert result["size_bytes"] > 0

    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "nahlah-media"
    assert kwargs["Key"].startswith("template-headers/7/")
    assert kwargs["ContentType"] == "image/webp"
    assert kwargs["Metadata"] == {
        "tenant-id": "7",
        "status": "attached",
        "purpose": "whatsapp-template-header",
    }


@patch("services.template_media_storage._s3_client")
def test_invalid_template_header_never_reaches_storage(mock_client_factory):
    with pytest.raises(cms.CatalogMediaValidationError, match="unsupported_image_type"):
        tms.upload_template_header_image(tenant_id=7, content=b"not-an-image")

    mock_client_factory.assert_not_called()
