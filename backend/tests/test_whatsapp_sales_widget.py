"""Storefront WhatsApp widget parity and merchant image storage."""
from __future__ import annotations

from io import BytesIO
import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image

from routers.widgets import (
    _build_nahla_widgets_js,
    _safe_widget_image_url,
    serve_salla_auto_snippet,
    serve_whatsapp_bee_image,
    serve_widgets_js_by_salla,
)
from services import widget_media_storage as media


def test_widget_bundle_matches_store_sizes_and_uses_safe_image_nodes():
    widget = {
        "widget_key": "whatsapp_widget",
        "is_enabled": True,
        "settings": {"phone": "966555906901", "logo_url": "https://merchant.example/logo.png"},
        "display_rules": {"trigger": "scroll"},
    }
    js = _build_nahla_widgets_js([widget])

    for declaration in (
        "width:110px;height:110px",
        "width:90px;height:90px",
        "width:65px;height:65px",
        "width:58px;height:58px",
        "bottom:55px",
        "bottom:50px",
        "bee-float 3s",
        "apple-wave 2.8s",
        "for(var orbit=1;orbit<=4;orbit++)",
        "window.scrollY<=threshold",
        "brand.src=logo",
        "pixels.data[pixel]>210&&pixels.data[pixel+1]>210&&pixels.data[pixel+2]>210",
    ):
        assert declaration in js
    assert "wrap.innerHTML" not in js
    assert "merchant.example/logo.png" in js


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:image/png;base64,AA", "http://example.com/a.png", "https://localhost/a.png", "https://127.0.0.1/a.png"])
def test_widget_image_url_requires_public_https(url):
    with pytest.raises(ValueError, match="invalid_widget_image_url"):
        _safe_widget_image_url(url)


def test_uploaded_logo_is_tenant_scoped_and_keeps_png_alpha(monkeypatch):
    class Storage:
        saved = None

        def put_object(self, **kwargs):
            self.saved = kwargs

    storage = Storage()
    monkeypatch.setattr(media, "_s3_client", lambda: storage)
    monkeypatch.setattr(media, "is_catalog_media_storage_configured", lambda: True)
    monkeypatch.setattr(media, "catalog_media_public_base_url", lambda: "https://media.example")
    monkeypatch.setattr(media, "catalog_media_bucket", lambda: "test-bucket")

    output = BytesIO()
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(output, format="PNG")
    result = media.upload_widget_logo(tenant_id=42, content=output.getvalue())

    assert result["image_url"].startswith("https://media.example/widget-logos/42/")
    assert result["image_url"].endswith(".png")
    assert storage.saved["ContentType"] == "image/png"
    assert storage.saved["Metadata"]["tenant-id"] == "42"
    with Image.open(BytesIO(storage.saved["Body"])) as saved:
        assert saved.mode == "RGBA"


def test_salla_store_route_resolves_current_external_store_id():
    from models import Integration, MerchantWidget

    class Query:
        def __init__(self, model):
            self.model = model

        def filter(self, *conditions):
            if self.model is Integration:
                statement = " ".join(str(item) for item in conditions)
                assert "external_store_id" in statement
            return self

        def first(self):
            return SimpleNamespace(tenant_id=42)

        def all(self):
            return [SimpleNamespace(widget_key="whatsapp_widget", is_enabled=True,
                                    settings_json={"phone": "966555906901"}, display_rules={})]

    class DB:
        def query(self, model):
            assert model in {Integration, MerchantWidget}
            return Query(model)

    response = asyncio.run(serve_widgets_js_by_salla("1298199463", DB()))
    assert b"966555906901" in response.body
    assert b"/merchant/widgets/assets/whatsapp-bee.jpg" in response.body


def test_original_store_logo_is_served_for_canvas_with_cors():
    response = asyncio.run(serve_whatsapp_bee_image())
    assert response.media_type == "image/jpeg"
    assert response.headers["access-control-allow-origin"] == "*"
    with Image.open(BytesIO(response.body)) as image:
        assert image.size == (1000, 666)


def test_salla_loader_detects_theme_store_class():
    response = asyncio.run(serve_salla_auto_snippet())
    assert b"themeStoreClass" in response.body
    assert b"data-nahla-store-bundle" in response.body
