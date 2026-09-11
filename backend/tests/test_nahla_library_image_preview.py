"""The library modal must receive the IMAGE source, not just BODY text."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from core.commerce_lifecycle.order_confirmation_assets import (
    ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL,
)
from core.commerce_lifecycle.cod_confirmation_assets import (
    COD_CONFIRMATION_HEADER_DEFAULT_URL,
)
from services.whatsapp_templates.nahla_templates import (
    get_all_templates,
    get_template_by_key,
    template_preview,
)


def test_order_summary_preview_preserves_image_body_footer_and_button():
    definition = get_template_by_key("order_summary")
    before = deepcopy(definition)
    preview = template_preview(definition)
    assert preview["header_type"] == "image"
    assert preview["preview_header_image_url"] == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL
    assert preview["preview_body"] == next(
        c["text"] for c in definition["components"] if c["type"] == "BODY"
    )
    assert preview["preview_footer"] == ""
    assert preview["buttons"][0]["url"] == "https://mtjr.at/{{1}}"
    assert definition == before


def test_component_custom_image_is_not_replaced_by_platform_default():
    definition = deepcopy(get_template_by_key("order_summary"))
    image = next(c for c in definition["components"] if c["type"] == "HEADER")
    image["example"]["header_url"] = "https://merchant.example/order-confirmed.jpg"
    assert template_preview(definition)["preview_header_image_url"] == image["example"]["header_url"]


def test_cod_confirmation_preview_uses_its_own_image():
    definition = get_template_by_key("cod_confirmation")
    preview = template_preview(definition)
    assert preview["header_type"] == "image"
    assert preview["preview_header_image_url"] == COD_CONFIRMATION_HEADER_DEFAULT_URL


def test_text_only_order_confirmation_gets_no_image_fallback():
    definition = deepcopy(get_template_by_key("order_summary"))
    definition["components"] = [c for c in definition["components"] if c["type"] != "HEADER"]
    preview = template_preview(definition)
    assert "header_type" not in preview
    assert "preview_header_image_url" not in preview


def test_other_library_services_do_not_gain_lifecycle_image():
    for definition in get_all_templates():
        if definition.get("service_key") in {"order_confirmation", "cod_confirmation"}:
            continue
        preview = template_preview(definition)
        assert "header_type" not in preview, definition["key"]
        assert "preview_header_image_url" not in preview, definition["key"]


@pytest.mark.parametrize("tenant_id", [101, 202])
def test_authenticated_library_projects_tenant_image_into_flat_and_grouped_results(monkeypatch, tenant_id):
    from core import merchant_capabilities as capabilities
    from routers import templates as router
    from services.whatsapp_templates import template_capability_filter as filters

    definition = deepcopy(get_template_by_key("order_summary"))
    before = deepcopy(definition)
    definition["filter_meta"] = {"order_channel": "external_store"}
    merchant_image = f"https://merchant{tenant_id}.example/order-confirmed.jpg"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        extra_metadata={"order_updates": {"order_confirmation": {
            "runtime": {"header_image_url": merchant_image},
        }}},
    )
    monkeypatch.setattr(capabilities, "capability_aware_templates_enabled", lambda: True)
    monkeypatch.setattr(router, "resolve_tenant_id", lambda request: tenant_id)
    monkeypatch.setattr(capabilities, "resolve_merchant_capabilities", lambda db, tid: object())
    monkeypatch.setattr(capabilities, "read_default_order_channel", lambda db, tid: "external_store")
    monkeypatch.setattr(filters, "filter_and_group_library_templates", lambda *args, **kwargs: {
        "templates": [definition],
        "groups": [{"channel": "external_store", "templates": [definition]}],
    })
    request = Request({"type": "http", "method": "GET", "path": "/templates/nahla-library", "headers": []})
    result = asyncio.run(router.get_nahla_library(request, db=db))
    for preview in [result["templates"][0], result["groups"][0]["templates"][0]]:
        assert preview["header_type"] == "image"
        assert preview["preview_header_image_url"] == merchant_image
        assert preview["filter_meta"]["order_channel"] == "external_store"
    assert definition["components"] == before["components"]
    assert template_preview(get_template_by_key("order_summary"))["preview_header_image_url"] == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL
    db.commit.assert_not_called()


def test_legacy_public_library_uses_platform_image_without_tenant_lookup(monkeypatch):
    from core import merchant_capabilities as capabilities
    from routers import templates as router

    monkeypatch.setattr(capabilities, "capability_aware_templates_enabled", lambda: False)
    db = MagicMock()
    request = Request({"type": "http", "method": "GET", "path": "/templates/nahla-library", "headers": []})
    result = asyncio.run(router.get_nahla_library(request, db=db))
    preview = next(t for t in result["templates"] if t["key"] == "order_summary")
    assert preview["preview_header_image_url"] == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL
    db.query.assert_not_called()
