"""Verify WhatsApp connect Graph asset queries use Bearer auth only."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
for entry in (str(REPO), str(REPO / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from routers.whatsapp_connect import _fetch_phone_number_info, _fetch_waba_info  # noqa: E402

TOKEN = "user-long-token-asset-877"
WABA = "TEST-WABA-ASSET-877"
PHONE = "TEST-PHONE-ASSET-877"


@pytest.fixture()
def capture_transport(monkeypatch):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": request.url.path.split("/")[-1]})

    transport = httpx.MockTransport(handler)
    orig_async = httpx.AsyncClient

    class _AsyncClient(orig_async):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    return captured


def test_connect_asset_queries_use_bearer_header_only(capture_transport):
    asyncio.run(_fetch_waba_info(TOKEN, WABA))
    asyncio.run(_fetch_phone_number_info(TOKEN, PHONE))
    assert len(capture_transport) == 2
    for req in capture_transport:
        assert req.method == "GET"
        assert "access_token" not in req.url.params
        assert TOKEN not in str(req.url)
        assert req.headers.get("authorization") == f"Bearer {TOKEN}"
        assert list(req.url.params.keys()) == ["fields"]
