"""Saved order-summary previews must receive a browser-loadable IMAGE URL."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from routers import templates as router


def _template(
    *,
    service_key: str,
    components: list[dict],
    ai_generation_metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        tenant_id=7,
        meta_template_id=None,
        name="nahla_order_summary_preview",
        language="ar",
        category="UTILITY",
        status="DRAFT",
        rejection_reason=None,
        components=components,
        created_at=None,
        updated_at=None,
        synced_at=None,
        source="nahla",
        objective=None,
        usage_count=0,
        last_used_at=None,
        health_score=None,
        recommendation_state="none",
        recommendation_note=None,
        ai_generation_metadata=ai_generation_metadata or {},
        service_key=service_key,
        display_name_ar="ملخص الطلب",
        nahla_source_key="order_summary",
        is_active=False,
        is_hidden=False,
        step_number=None,
        has_coupon=False,
        trigger_delay_hours=None,
    )


def test_saved_order_summary_projects_resolved_image_url(monkeypatch):
    url = "https://merchant.example/summary.jpg"
    resolver = MagicMock(return_value=url)
    monkeypatch.setattr(
        "core.commerce_lifecycle.order_confirmation_meta_header.resolve_order_confirmation_preview_header_url",
        resolver,
    )
    template = _template(
        service_key="order_confirmation",
        components=[{"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["meta-h"]}}],
    )

    payload = router._tpl_to_dict(template, db=MagicMock())

    header = payload["components"][0]
    assert header["example"]["header_handle"] == ["meta-h"]
    assert header["example"]["header_url"] == url
    resolver.assert_called_once()


def test_saved_text_only_stays_text_and_cod_gets_its_own_image():
    from core.commerce_lifecycle.cod_confirmation_assets import (
        COD_CONFIRMATION_HEADER_DEFAULT_URL,
    )

    image_components = [{"type": "HEADER", "format": "IMAGE"}]
    text_only = _template(service_key="order_confirmation", components=[{"type": "BODY", "text": "تم"}])
    cod = _template(service_key="cod_confirmation", components=image_components)

    assert "header_url" not in router._tpl_to_dict(text_only, db=MagicMock())["components"][0].get("example", {})
    assert (
        router._tpl_to_dict(cod, db=MagicMock())["components"][0]["example"]["header_url"]
        == COD_CONFIRMATION_HEADER_DEFAULT_URL
    )


def test_damaged_managed_cod_draft_recovers_missing_image_header():
    from core.commerce_lifecycle.cod_confirmation_assets import (
        COD_CONFIRMATION_HEADER_ASSET_KEY,
        COD_CONFIRMATION_HEADER_DEFAULT_URL,
    )

    damaged = _template(
        service_key="cod_confirmation",
        components=[{"type": "BODY", "text": "هل تريد تأكيد الطلب؟"}],
        ai_generation_metadata={
            "header_image_asset_key": COD_CONFIRMATION_HEADER_ASSET_KEY,
        },
    )

    payload = router._tpl_to_dict(damaged, db=MagicMock())

    assert payload["components"][0] == {
        "type": "HEADER",
        "format": "IMAGE",
        "example": {
            "header_url": COD_CONFIRMATION_HEADER_DEFAULT_URL,
            "header_handle": [],
        },
    }
    assert payload["components"][1]["type"] == "BODY"


def test_managed_cod_edit_keeps_image_and_discards_text_header_replacement():
    from core.commerce_lifecycle.cod_confirmation_assets import (
        COD_CONFIRMATION_HEADER_ASSET_KEY,
    )

    damaged = _template(
        service_key="cod_confirmation",
        components=[{"type": "BODY", "text": "قديم"}],
        ai_generation_metadata={
            "header_image_asset_key": COD_CONFIRMATION_HEADER_ASSET_KEY,
        },
    )

    components = router._restore_managed_cod_image_header(
        damaged,
        [
            {"type": "HEADER", "format": "TEXT", "text": "عنوان بديل"},
            {"type": "BODY", "text": "جديد"},
        ],
    )

    assert [component["type"] for component in components] == ["HEADER", "BODY"]
    assert components[0]["format"] == "IMAGE"
    assert components[1]["text"] == "جديد"


def test_managed_cod_edit_keeps_merchant_uploaded_image():
    from core.commerce_lifecycle.cod_confirmation_assets import (
        COD_CONFIRMATION_HEADER_ASSET_KEY,
    )

    merchant_url = "https://media.example/template-headers/7/custom.webp"
    template = _template(
        service_key="cod_confirmation",
        components=[
            {
                "type": "HEADER",
                "format": "IMAGE",
                "example": {"header_url": merchant_url, "header_handle": []},
            },
            {"type": "BODY", "text": "قديم"},
        ],
        ai_generation_metadata={
            "header_image_asset_key": COD_CONFIRMATION_HEADER_ASSET_KEY,
            "merchant_header_image_url": merchant_url,
            "header_image_source": "merchant_upload",
        },
    )

    components = router._restore_managed_cod_image_header(
        template,
        [{"type": "BODY", "text": "تم تعديل النص"}],
    )

    assert components[0]["example"]["header_url"] == merchant_url
    assert components[1]["text"] == "تم تعديل النص"


def test_header_upload_replaces_image_and_persists_merchant_source(monkeypatch):
    from core.commerce_lifecycle.cod_confirmation_assets import (
        COD_CONFIRMATION_HEADER_ASSET_KEY,
    )
    from services.catalog_media_storage import MAX_UPLOAD_BYTES

    template = _template(
        service_key="cod_confirmation",
        components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "هل تريد تأكيد الطلب؟"},
        ],
        ai_generation_metadata={
            "header_image_asset_key": COD_CONFIRMATION_HEADER_ASSET_KEY,
        },
    )
    query = MagicMock()
    query.filter.return_value.first.return_value = template
    db = MagicMock()
    db.query.return_value = query
    monkeypatch.setattr(router, "resolve_tenant_id", lambda _request: 7)
    merchant_url = "https://media.example/template-headers/7/new.webp"
    monkeypatch.setattr(
        "services.template_media_storage.upload_template_header_image",
        lambda **_kwargs: {"image_url": merchant_url},
    )

    class Upload:
        read_size = None

        async def read(self, size):
            self.read_size = size
            return b"valid-image-bytes"

    upload = Upload()
    result = asyncio.run(
        router.upload_template_header_image(
            template_id=template.id,
            request=MagicMock(),
            file=upload,
            db=db,
        )
    )

    assert upload.read_size == MAX_UPLOAD_BYTES + 1
    assert result["image_url"] == merchant_url
    assert template.components[0]["example"] == {
        "header_url": merchant_url,
        "header_handle": [],
    }
    assert template.ai_generation_metadata["header_image_source"] == "merchant_upload"
    assert template.ai_generation_metadata["merchant_header_image_url"] == merchant_url
    db.commit.assert_called_once()
