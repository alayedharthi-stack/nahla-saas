"""SSRF-safe header image fetch for order_confirmation Meta submit."""
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

from core.commerce_lifecycle.order_confirmation_header_image_fetch import (  # noqa: E402
    HeaderImageFetchError,
    MAX_HEADER_IMAGE_BYTES,
    fetch_header_image_bytes_secure,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeStreamResponse:
    def __init__(self, *, status: int, headers: dict, body: bytes) -> None:
        self.status_code = status
        self.headers = headers
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class TestSecureHeaderImageFetch:
    def test_blocks_internal_https_url(self):
        with pytest.raises(HeaderImageFetchError) as exc:
            _run(fetch_header_image_bytes_secure("https://127.0.0.1/secret.jpg"))
        assert exc.value.error_code in {"host_blocked", "ip_blocked", "destination_blocked"}

    def test_blocks_railway_internal_host(self):
        with pytest.raises(HeaderImageFetchError) as exc:
            _run(
                fetch_header_image_bytes_secure(
                    "https://postgres-ancu.railway.internal/header.jpg"
                )
            )
        assert exc.value.error_code == "host_blocked"

    def test_blocks_redirect_to_internal(self):
        public = "https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/a.jpg"
        internal = "http://169.254.169.254/latest/meta-data"
        redirect_resp = _FakeStreamResponse(
            status=302,
            headers={"location": internal},
            body=b"",
        )

        async def _fake_get(url):
            if url == public:
                return redirect_resp
            raise AssertionError(f"unexpected fetch {url}")

        client = MagicMock()
        client.get = AsyncMock(side_effect=_fake_get)
        client.aclose = AsyncMock()

        with pytest.raises(HeaderImageFetchError):
            _run(fetch_header_image_bytes_secure(public, client=client))

    def test_blocks_large_body(self):
        jpeg = b"\xff\xd8\xff" + b"x" * (MAX_HEADER_IMAGE_BYTES + 1)
        ok_resp = _FakeStreamResponse(
            status=200,
            headers={"content-type": "image/jpeg"},
            body=jpeg,
        )
        client = MagicMock()
        client.get = AsyncMock(return_value=ok_resp)
        client.aclose = AsyncMock()

        url = "https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/huge.jpg"
        with patch(
            "core.commerce_lifecycle.order_confirmation_header_image_fetch.validate_destination",
            return_value=(MagicMock(), ""),
        ):
            with pytest.raises(HeaderImageFetchError) as exc:
                _run(fetch_header_image_bytes_secure(url, client=client))
        assert exc.value.error_code == "body_too_large"

    def test_blocks_html_response(self):
        html = b"<html><body>nope</body></html>"
        ok_resp = _FakeStreamResponse(
            status=200,
            headers={"content-type": "text/html"},
            body=html,
        )
        client = MagicMock()
        client.get = AsyncMock(return_value=ok_resp)
        client.aclose = AsyncMock()
        url = "https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/fake.jpg"
        with patch(
            "core.commerce_lifecycle.order_confirmation_header_image_fetch.validate_destination",
            return_value=(MagicMock(), ""),
        ):
            with pytest.raises(HeaderImageFetchError) as exc:
                _run(fetch_header_image_bytes_secure(url, client=client))
        assert exc.value.error_code == "html_response_blocked"

    def test_accepts_valid_jpeg(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"img"
        ok_resp = _FakeStreamResponse(
            status=200,
            headers={"content-type": "image/jpeg"},
            body=jpeg,
        )
        client = MagicMock()
        client.get = AsyncMock(return_value=ok_resp)
        client.aclose = AsyncMock()
        url = "https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/ok.jpg"
        with patch(
            "core.commerce_lifecycle.order_confirmation_header_image_fetch.validate_destination",
            return_value=(MagicMock(), ""),
        ):
            body, mime = _run(fetch_header_image_bytes_secure(url, client=client))
        assert mime == "image/jpeg"
        assert body == jpeg
