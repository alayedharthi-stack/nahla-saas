"""Saved order-summary previews must receive a browser-loadable IMAGE URL."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from routers import templates as router


def _template(*, service_key: str, components: list[dict]) -> SimpleNamespace:
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
        ai_generation_metadata={},
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
