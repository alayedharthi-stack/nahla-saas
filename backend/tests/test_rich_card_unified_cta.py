"""Option A: unified interactive.cta_url product card (image header + facts + CTA)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_here = Path(__file__).resolve().parent
_backend = _here.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from modules.observability.delivery_mode import (  # noqa: E402
    compute_final_delivery_mode,
    new_delivery_audit,
)
from routers.whatsapp_webhook import (  # noqa: E402
    _product_card_factual_body,
    _safe_cta_http_url,
    build_cta_url_payload,
)


def _att(**kw) -> Dict[str, Any]:
    base = {
        "kind": "product_card",
        "id": 28,
        "title": "جاكيت شتوي أسود",
        "caption": "جاكيت شتوي أسود\nالسعر: 320 ر.س",
        "file_url": "https://cdn.example/jacket.jpg",
        "product_url": "https://shop.example/p/jacket",
        "price": "320",
        "external_id": "ext-28",
        "needs_variant_choice": False,
        "variants": [],
    }
    base.update(kw)
    return base


class TestBuildCtaUrlPayload:
    def test_image_plus_url_unified_payload(self) -> None:
        payload = build_cta_url_payload(
            to="966555906901",
            body_text="جاكيت شتوي أسود\nالسعر: 320 ر.س",
            btn_label="عرض المنتج",
            btn_url="https://shop.example/p/jacket",
            header_image_url="https://cdn.example/jacket.jpg",
        )
        assert payload is not None
        assert payload["type"] == "interactive"
        interactive = payload["interactive"]
        assert interactive["type"] == "cta_url"
        assert interactive["header"]["type"] == "image"
        assert interactive["header"]["image"]["link"] == "https://cdn.example/jacket.jpg"
        assert interactive["body"]["text"].startswith("جاكيت")
        assert "اضغط زر" not in interactive["body"]["text"]
        assert interactive["action"]["parameters"]["url"] == "https://shop.example/p/jacket"
        assert interactive["action"]["parameters"]["display_text"] == "عرض المنتج"

    def test_url_only_no_image_header(self) -> None:
        payload = build_cta_url_payload(
            to="966500000001",
            body_text="عطر ورد 100ml",
            btn_label="عرض المنتج",
            btn_url="https://shop.example/p/perfume",
        )
        assert payload is not None
        assert "header" not in payload["interactive"]
        assert payload["interactive"]["body"]["text"] == "عطر ورد 100ml"

    def test_invalid_tel_url_rejected(self) -> None:
        assert (
            build_cta_url_payload(
                to="966500000001",
                body_text="x",
                btn_label="عرض المنتج",
                btn_url="tel:+966555000000",
            )
            is None
        )
        assert _safe_cta_http_url("tel:+966555000000") == ""

    def test_invalid_header_image_omitted_not_reject_url(self) -> None:
        payload = build_cta_url_payload(
            to="966500000001",
            body_text="title",
            btn_label="عرض المنتج",
            btn_url="https://shop.example/p/ok",
            header_image_url="tel:+966555000000",
        )
        assert payload is not None
        assert "header" not in payload["interactive"]


class TestFactualBody:
    def test_prefers_caption(self) -> None:
        body = _product_card_factual_body(_att())
        assert "جاكيت" in body
        assert "اضغط" not in body

    def test_title_price_fallback(self) -> None:
        body = _product_card_factual_body(
            _att(caption="", title="حذاء رياضي أبيض", price="220")
        )
        assert "حذاء رياضي أبيض" in body
        assert "220" in body


class TestDeliveryModeUnified:
    def test_unified_card_classifies_as_image_cta(self) -> None:
        a = new_delivery_audit()
        a["text_sent"] = True
        a["unified_product_card_sent_count"] = 1
        assert compute_final_delivery_mode(a) == "image_cta"
        # Must not look like two separate legacy payloads.
        assert int(a.get("legacy_media_sent_count") or 0) == 0
        assert int(a.get("cta_url_sent_count") or 0) == 0

    def test_image_only_still_media_only(self) -> None:
        a = new_delivery_audit()
        a["legacy_media_sent_count"] = 1
        assert compute_final_delivery_mode(a) == "media_only"

    def test_url_only_still_cta_only(self) -> None:
        a = new_delivery_audit()
        a["cta_url_sent_count"] = 1
        assert compute_final_delivery_mode(a) == "cta_only"


class TestVariantSequencingWithUnifiedCard:
    def test_variant_prompt_after_unified_card(self) -> None:
        import asyncio

        from routers import whatsapp_webhook as wh

        att = _att(
            needs_variant_choice=True,
            variants=[
                {"id": 1, "option_summary": "M", "in_stock": True},
                {"id": 2, "option_summary": "L", "in_stock": True},
            ],
        )
        sent: list[str] = []

        async def _fake_cta(*_a, **kwargs):
            sent.append("unified")
            assert kwargs.get("header_image_url")
            assert "اضغط زر" not in str(kwargs.get("body_text") or "")
            return True

        async def _fake_text(*_a, **_k):
            sent.append("variant")
            return True

        async def _run() -> None:
            with patch.object(wh, "_send_cta_url", new=AsyncMock(side_effect=_fake_cta)):
                with patch.object(
                    wh, "_send_whatsapp_message", new=AsyncMock(side_effect=_fake_text)
                ):
                    audit = new_delivery_audit()
                    await wh._send_cta_url(
                        phone_id="1",
                        to="966500000001",
                        body_text=att["caption"],
                        btn_label="عرض المنتج",
                        btn_url=att["product_url"],
                        header_image_url=att["file_url"],
                    )
                    await wh._maybe_send_variant_prompt_after_product_card(
                        db=None,
                        tenant_id=21,
                        phone_id="1",
                        to="966500000001",
                        attachment=att,
                        delivery_audit=audit,
                    )
                    assert sent == ["unified", "variant"]
                    assert int(audit.get("variant_prompt_sent_count") or 0) >= 1

        asyncio.run(_run())


class TestSendCtaUrlPostsUnifiedPayload:
    def test_post_wa_receives_header_image(self) -> None:
        import asyncio

        from routers import whatsapp_webhook as wh

        captured: list[dict] = []

        async def _fake_post(_phone_id, payload, **_k):
            captured.append(payload)
            return True

        async def _run() -> None:
            with patch.object(wh, "_post_wa", new=AsyncMock(side_effect=_fake_post)):
                ok = await wh._send_cta_url(
                    phone_id="pid",
                    to="966500000001",
                    body_text="قميص قطني أزرق\nالسعر: 129 ر.س",
                    btn_label="عرض المنتج",
                    btn_url="https://shop.example/p/shirt",
                    header_image_url="https://cdn.example/shirt.jpg",
                )
            assert ok is True
            assert len(captured) == 1
            interactive = captured[0]["interactive"]
            assert interactive["header"]["image"]["link"].endswith("shirt.jpg")
            assert "اضغط زر" not in interactive["body"]["text"]

        asyncio.run(_run())
