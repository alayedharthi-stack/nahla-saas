"""JWT public-prefix coverage for WhatsApp outbound media file URLs."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request

from core.middleware import JWT_PUBLIC_PREFIXES, jwt_enforcement_middleware


def _is_jwt_public(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in JWT_PUBLIC_PREFIXES)


def test_ai_media_file_stream_path_is_jwt_public() -> None:
    assert _is_jwt_public("/intelligence/ai-media/file/1")
    assert _is_jwt_public("/intelligence/ai-media/file/999")


@pytest.mark.parametrize(
    "path",
    [
        "/intelligence/ai-media",
        "/intelligence/ai-media/upload",
        "/intelligence/ai-media/keys",
        "/intelligence/ai-media/42",
        "/intelligence/ai-media/42/toggle",
    ],
)
def test_other_intelligence_routes_remain_jwt_protected(path: str) -> None:
    assert not _is_jwt_public(path)


def test_jwt_middleware_passes_through_ai_media_file_without_token() -> None:
    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/intelligence/ai-media/file/7",
                "headers": [],
                "query_string": b"",
            }
        )
        downstream = AsyncMock(return_value=MagicMock(status_code=404))
        response = await jwt_enforcement_middleware(request, downstream)
        downstream.assert_awaited_once()
        assert response.status_code == 404

    asyncio.run(_run())


def test_jwt_middleware_blocks_intelligence_list_without_token() -> None:
    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/intelligence/ai-media",
                "headers": [],
                "query_string": b"",
            }
        )
        downstream = AsyncMock()
        response = await jwt_enforcement_middleware(request, downstream)
        downstream.assert_not_awaited()
        assert response.status_code == 401
        assert response.body == b'{"detail":"Authentication required","code":"missing_token"}'

    asyncio.run(_run())
