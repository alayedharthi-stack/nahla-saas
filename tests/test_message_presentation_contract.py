"""Contract coverage for the transport-neutral conversation presentation."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.message_presentation import (  # noqa: E402
    PRESENTATION_VERSION,
    RESPONSE_BUNDLE_VERSION,
    normalise_response_bundle,
    presentation_from_provider_payload,
    response_bundle_for_message_event,
)


class _Query:
    def __init__(self, value):
        self.value = value

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.value


class _FixtureDb:
    def __init__(self, *, template=None, variant=None, product=None):
        self.template = template
        self.variant = variant
        self.product = product

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "WhatsAppTemplate":
            return _Query(self.template)
        if name == "ProductVariant":
            return _Query(self.variant)
        if name == "Product":
            return _Query(self.product)
        return _Query(None)


def test_plain_and_multiline_text_contract():
    presentation = presentation_from_provider_payload({
        "type": "text",
        "text": {"body": "السطر الأول\nSecond line"},
    })
    assert presentation == {
        "version": PRESENTATION_VERSION,
        "kind": "text",
        "body": "السطر الأول\nSecond line",
        "text_direction": "auto",
        "actions": [],
    }


def test_image_caption_contract():
    presentation = presentation_from_provider_payload({
        "type": "image",
        "image": {"link": "https://cdn.example.test/item.jpg", "caption": "قميص أزرق"},
    })
    assert presentation["kind"] == "media"
    assert presentation["body"] == "قميص أزرق"
    assert presentation["media"]["kind"] == "image"
    assert presentation["media"]["url"].endswith("item.jpg")


def test_product_card_uses_tenant_scoped_persisted_catalog_facts():
    variant = SimpleNamespace(
        product_id=91,
        image_url="https://cdn.example.test/shirt.jpg",
        price="129.00",
        currency="SAR",
        in_stock=True,
    )
    product = SimpleNamespace(
        id=91,
        title="قميص قطني",
        price="130.00",
        in_stock=True,
        extra_metadata={"product_url": "https://shop.example.test/p/91"},
    )
    presentation = presentation_from_provider_payload(
        {
            "type": "interactive",
            "interactive": {
                "type": "product",
                "body": {"text": "هذا هو المنتج"},
                "action": {"catalog_id": "cat", "product_retailer_id": "sku-91"},
            },
        },
        db=_FixtureDb(variant=variant, product=product),
        tenant_id=7,
    )
    assert presentation["kind"] == "product"
    assert presentation["product"] == {
        "id": "91",
        "retailer_id": "sku-91",
        "name": "قميص قطني",
        "image_url": "https://cdn.example.test/shirt.jpg",
        "price": "129.00",
        "currency": "SAR",
        "availability": True,
        "url": "https://shop.example.test/p/91",
    }
    assert presentation["actions"][0]["kind"] == "open_product"


def test_template_preserves_header_image_footer_and_buttons():
    template = SimpleNamespace(
        category="UTILITY",
        language="ar",
        service_key=None,
        components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "مرحباً {{1}}"},
            {"type": "FOOTER", "text": "شكراً لك"},
            {"type": "BUTTONS", "buttons": [
                {"type": "QUICK_REPLY", "text": "تم"},
                {"type": "URL", "text": "التتبع", "url": "https://shop.example.test/{{1}}"},
            ]},
        ],
    )
    presentation = presentation_from_provider_payload(
        {
            "type": "template",
            "template": {
                "name": "shipping_update_ar",
                "language": {"code": "ar"},
                "components": [
                    {"type": "header", "parameters": [{
                        "type": "image", "image": {"link": "https://cdn.example.test/header.jpg"},
                    }]},
                    {"type": "body", "parameters": [{"type": "text", "text": "نورة"}]},
                    {"type": "button", "sub_type": "url", "index": "1", "parameters": [
                        {"type": "text", "text": "track-1"},
                    ]},
                ],
            },
        },
        db=_FixtureDb(template=template),
        tenant_id=7,
    )
    assert presentation["body"] == "مرحباً نورة"
    assert presentation["media"]["url"].endswith("header.jpg")
    assert presentation["template"]["footer"] == "شكراً لك"
    assert [action["kind"] for action in presentation["actions"]] == ["quick_reply", "url"]
    assert presentation["actions"][1]["url"].endswith("track-1")


def test_order_confirmation_legacy_row_is_visually_identifiable():
    template = SimpleNamespace(
        name="order_confirmed_ar",
        category="UTILITY",
        language="ar",
        service_key="order_confirmed",
        components=[
            {"type": "BODY", "text": "تم تأكيد الطلب {{1}}"},
            {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "عرض الطلب"}]},
        ],
    )
    row = SimpleNamespace(
        tenant_id=7,
        body="تم تأكيد الطلب #A-42",
        event_type="cod_confirmation",
        extra_metadata={"template_name": template.name, "order_id": 42},
    )
    bundle = response_bundle_for_message_event(
        row,
        db=_FixtureDb(template=template),
        template_lookup={template.name: template},
    )
    item = bundle["presentations"][0]
    assert item["kind"] == "order_lifecycle"
    assert item["template"]["service_key"] == "order_confirmed"
    assert item["order"]["id"] == "42"
    assert item["actions"][0]["kind"] == "quick_reply"


def test_failed_media_and_old_message_degrade_without_fabrication():
    media_row = SimpleNamespace(
        body="[image]",
        extra_metadata={"delivery_status": "failed"},
    )
    bundle = response_bundle_for_message_event(
        media_row,
        media_block={
            "kind": "image",
            "storage_url": None,
            "download_status": "failed",
            "error": "object_missing",
        },
    )
    assert bundle["delivery"]["state"] == "failed"
    assert bundle["presentations"][0]["media"]["url"] is None
    assert bundle["presentations"][0]["media"]["error"] == "object_missing"

    legacy = response_bundle_for_message_event(
        SimpleNamespace(body="legacy text only", extra_metadata=None),
    )
    assert legacy["presentations"] == [{
        "version": PRESENTATION_VERSION,
        "kind": "text",
        "body": "legacy text only",
        "text_direction": "auto",
        "actions": [],
    }]


def test_unknown_fields_are_removed_when_reading_stored_json():
    bundle = normalise_response_bundle({
        "version": "future",
        "presentations": [{
            "version": "future",
            "kind": "text",
            "body": "safe",
            "unsafe": "ignored",
            "actions": [{"kind": "not-real", "label": "ignored"}],
        }],
    })
    assert bundle["version"] == RESPONSE_BUNDLE_VERSION
    assert bundle["presentations"][0]["version"] == PRESENTATION_VERSION
    assert bundle["presentations"][0]["actions"] == []
    assert "unsafe" not in bundle["presentations"][0]
