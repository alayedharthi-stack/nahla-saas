"""Meta submit payload for order_confirmation IMAGE HEADER."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

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

    def test_ensure_uploads_and_strips_header_url(self, monkeypatch: pytest.MonkeyPatch):
        class _FakeUploader:
            async def upload_template_header(self, *, access_token, image_bytes, mime_type):
                assert access_token == "token"
                assert image_bytes == b"jpeg-bytes"
                assert mime_type == "image/jpeg"
                return "4::uploaded_handle"

        class _FakeCtx:
            access_token = "token"

        async def _fake_token(*_a, **_k):
            return _FakeCtx()

        async def _fake_fetch(url: str, *, timeout: float = 30.0):
            assert url
            return b"jpeg-bytes", "image/jpeg"

        monkeypatch.setattr(
            "services.whatsapp_platform.token_manager.get_token_for_operation",
            _fake_token,
        )
        monkeypatch.setattr(
            "core.commerce_lifecycle.order_confirmation_meta_header.fetch_header_image_bytes",
            _fake_fetch,
        )

        prepared = asyncio.run(
            ensure_order_confirmation_image_header_for_meta(
                db=None,
                conn=object(),
                tenant_id=1,
                components=order_summary_r3_components(),
                metadata={},
                uploader=_FakeUploader(),
            )
        )
        assert prepared[0]["example"]["header_handle"] == ["4::uploaded_handle"]
        assert "header_url" not in prepared[0]["example"]
