"""Meta submit payload for order_confirmation IMAGE HEADER."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.nahla_library_order_confirmation_import import (  # noqa: E402
    order_summary_r3_components,
)
from core.commerce_lifecycle.order_confirmation_assets import (  # noqa: E402
    ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL,
    order_confirmation_header_public_url,
)
from core.commerce_lifecycle.order_confirmation_meta_header import (  # noqa: E402
    ensure_order_confirmation_image_header_for_meta,
    prepare_order_confirmation_meta_submit_components,
    resolve_header_image_source_url,
    resolve_order_confirmation_preview_header_url,
)


class TestOrderConfirmationHeaderAsset:
    def test_default_url_is_verified_r2_not_dashboard_spa(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("NAHLA_ORDER_CONFIRMATION_HEADER_URL", raising=False)
        monkeypatch.delenv("NAHLA_DASHBOARD_PUBLIC_URL", raising=False)
        url = order_confirmation_header_public_url()
        assert url == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL
        assert "pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev" in url
        assert "app.nahlah.ai" not in url


class TestOrderConfirmationMetaSubmitPayload:
    def test_prepare_payload_uses_header_handle_only(self):
        handle = "4::nahla_test_handle"
        prepared = prepare_order_confirmation_meta_submit_components(
            order_summary_r3_components(),
            header_handle=handle,
        )
        header = prepared[0]
        assert header["type"] == "HEADER"
        assert header["format"] == "IMAGE"
        assert header["example"]["header_handle"] == [handle]
        assert "header_url" not in header["example"]

    def test_component_header_url_wins_over_metadata_r2(self):
        merchant = "https://cdn.merchant.example/custom-header.png"
        components = order_summary_r3_components()
        components[0]["example"]["header_url"] = merchant
        metadata = {"header_image_url": ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL}
        assert resolve_header_image_source_url(components, metadata) == merchant

    def test_r2_default_when_no_component_or_merchant_metadata(self):
        components = order_summary_r3_components()
        assert resolve_header_image_source_url(components, {}) == ORDER_CONFIRMATION_HEADER_R2_DEFAULT_URL

    def test_preview_helper_uses_component_header_url(self):
        merchant = "https://cdn.merchant.example/preview-header.jpg"
        components = order_summary_r3_components()
        components[0]["example"]["header_url"] = merchant
        url = resolve_order_confirmation_preview_header_url(
            MagicMock(),
            1,
            components,
            {},
        )
        assert url == merchant

    def test_ensure_uploads_and_strips_header_url(self, monkeypatch: pytest.MonkeyPatch):
        class _FakeUploader:
            async def upload_template_header(self, *, access_token, image_bytes, mime_type):
                assert access_token == "token"
                assert image_bytes == b"jpeg-bytes"
                assert mime_type == "image/jpeg"
                return "4::uploaded_handle"

        class _FakeCtx:
            token = "token"

        async def _fake_token(*_a, **_k):
            return _FakeCtx()

        async def _fake_fetch(url: str, **kwargs):
            assert url
            return b"jpeg-bytes", "image/jpeg"

        monkeypatch.setattr(
            "services.whatsapp_platform.token_manager.get_token_for_operation",
            _fake_token,
        )
        monkeypatch.setattr(
            "core.commerce_lifecycle.order_confirmation_meta_header.fetch_header_image_bytes_secure",
            _fake_fetch,
        )

        prepared = asyncio.run(
            ensure_order_confirmation_image_header_for_meta(
                db=MagicMock(),
                conn=object(),
                tenant_id=1,
                components=order_summary_r3_components(),
                metadata={},
                uploader=_FakeUploader(),
            )
        )
        assert prepared[0]["example"]["header_handle"] == ["4::uploaded_handle"]
        assert "header_url" not in prepared[0]["example"]

    def test_submit_hook_skipped_for_non_order_confirmation_service_key(self):
        from routers.templates import _submit_template_to_meta  # noqa: PLC0415

        with patch(
            "core.commerce_lifecycle.order_confirmation_meta_header.ensure_order_confirmation_image_header_for_meta",
            new_callable=AsyncMock,
        ) as ensure_mock:
            with patch(
                "routers.templates.provider_submit_template",
                new_callable=AsyncMock,
                return_value=({"id": "meta-1"}, MagicMock()),
            ):
                with patch("routers.templates._ensure_meta_examples", side_effect=lambda c: c):
                    asyncio.run(
                        _submit_template_to_meta(
                            db=MagicMock(),
                            conn=MagicMock(),
                            tenant_id=1,
                            waba_id="waba",
                            name="nahla_shipping",
                            language="ar",
                            category="UTILITY",
                            components=[{"type": "BODY", "text": "hi"}],
                            service_key="shipping_tracking",
                        )
                    )
        ensure_mock.assert_not_called()
